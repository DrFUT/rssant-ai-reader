"""业务逻辑层: 把 ai/ 的能力接到 rssant 的数据上。

这一层是唯一同时依赖 Django ORM 和 ai/ 服务层的地方,
Celery 任务与 HTTP 视图都只调用这里的函数, 便于复用与测试。
"""

import logging
from datetime import timedelta
from typing import Iterable, List, Optional

from django.db.models import Q
from django.utils import timezone

from ai import digest as ai_digest
from ai import interest as ai_interest
from ai.config import get_config
from ai.errors import AIError
from ai.prompts import DEFAULT_TOPIC, TOPICS
from ai.summarizer import fallback_summary, summarize_story

from rssant_api.models import STORY_SERVICE
from rssant_api.models.story_info import StoryInfo
from rssant_api.models.story_storage.common.story_key import StoryId

from .models import AIStatus, DailyDigest, StoryAI, UserInterest

LOG = logging.getLogger(__name__)

# PROCESSING 状态停留超过这个时长, 认为是 worker 被 kill 导致的僵尸行
STALE_PROCESSING_MINUTES = 15


def is_ai_enabled() -> bool:
    return get_config().is_enabled


# ---------------------------------------------------------------------------
# 文章 -> AI 摘要
# ---------------------------------------------------------------------------
def _load_story(story_id: int) -> Optional[StoryInfo]:
    return StoryInfo.objects.filter(id=story_id).first()


def _load_content(story: StoryInfo) -> str:
    """文章内容存在独立的 volume 表, 需要走 storage 读取。"""
    try:
        common = STORY_SERVICE.get_by_offset(story.feed_id, story.offset, detail=True)
    except Exception as ex:  # pragma: no cover - 防御性
        LOG.warning('load story content failed story=%s: %s', story.id, ex)
        return ''
    return getattr(common, 'content', None) or ''


def ensure_story_ai(story_ids: Iterable[int]) -> List[int]:
    """为一批文章创建 AI 待办记录, 返回新建记录的 id 列表。"""
    story_ids = [int(x) for x in story_ids if x]
    if not story_ids:
        return []
    exists = set(
        StoryAI.objects.filter(story_id__in=story_ids).values_list('story_id', flat=True)
    )
    new_ids = [x for x in story_ids if x not in exists]
    if not new_ids:
        return []
    rows = [StoryAI(story_id=x, status=AIStatus.PENDING) for x in new_ids]
    StoryAI.objects.bulk_create(rows, batch_size=100)
    return list(
        StoryAI.objects.filter(story_id__in=new_ids).values_list('id', flat=True)
    )


def on_new_stories(feed_id: int, storys: Iterable) -> dict:
    """RSS 同步完成后的钩子。

    只做两件很轻的事: 建待办记录 + 派发任务。
    LLM 调用本身永远不在这个同步链路里执行。
    """
    config = get_config()
    if not config.enable:
        return {'created': 0, 'dispatched': 0}
    storys = list(storys or [])
    if not storys:
        return {'created': 0, 'dispatched': 0}

    offsets = [s.offset for s in storys]
    # StoryInfo 的主键就是 StoryId.encode(feed_id, offset), 没有独立的
    # (feed_id, offset) 列可以 filter, 必须先算出候选主键再回查。
    candidate_ids = [StoryId.encode(feed_id, o) for o in offsets]
    existing_ids = set(
        StoryInfo.objects.filter(id__in=candidate_ids).values_list('id', flat=True)
    )
    story_ids = [i for i in candidate_ids if i in existing_ids]
    if not story_ids:
        return {'created': 0, 'dispatched': 0}

    new_ai_ids = ensure_story_ai(story_ids)
    # 低配保护: 单次同步最多派发 batch_size 个任务, 剩下的交给巡查任务
    dispatched = 0
    if config.is_enabled:
        from .tasks import summarize_story_task

        for ai_id in new_ai_ids[: config.batch_size]:
            try:
                summarize_story_task.delay(ai_id)
                dispatched += 1
            except Exception as ex:
                LOG.warning('dispatch ai task failed ai_id=%s: %s', ai_id, ex)
                break
    return {'created': len(new_ai_ids), 'dispatched': dispatched}


def claim_story_ai(story_ai_id: int, force: bool = False) -> bool:
    """原子抢占一条待办，只有抢到的执行者才允许调用 LLM。

    实现要点: 抢占由**单条带状态条件的 UPDATE** 完成 ——
        UPDATE rssant_ai_storyai SET status='processing'
        WHERE id = ? AND status IN ('pending', 'failed')
    并发执行时数据库行锁保证只有一个执行者拿到 updated=1，其余拿到 0。

    为什么不能用「先 SELECT 再判断 status != PROCESSING」:
    两个 worker 可以同时读到 pending，两边都判断为"可以处理"，
    于是同一篇文章被调用两次 LLM。只有把判定和改状态合成一条语句，
    原子性才由数据库而不是应用代码来保证。
    """
    statuses = [AIStatus.PENDING, AIStatus.FAILED]
    if force:
        # 手动刷新场景允许重跑已经完成的文章
        statuses.append(AIStatus.DONE)
    updated = StoryAI.objects.filter(
        id=story_ai_id, status__in=statuses
    ).update(status=AIStatus.PROCESSING, dt_updated=timezone.now())
    return updated == 1


def process_story_ai(story_ai_id: int, force: bool = False) -> bool:
    """处理一条 AI 待办: 调 LLM -> 写回数据库。返回是否成功。"""
    config = get_config()
    row = StoryAI.objects.filter(id=story_ai_id).first()
    if row is None:
        return False
    if not force and row.status == AIStatus.DONE:
        return True

    # 抢占失败说明: 别的执行者已经领走(processing)，或者状态已变成
    # done/skipped。两种情况都不该再调模型，直接退出。
    if not claim_story_ai(story_ai_id, force=force):
        LOG.info(
            'ai task not claimed ai_id=%s status=%s', story_ai_id, row.status,
        )
        return False

    story = _load_story(row.story_id)
    if story is None:
        row.status = AIStatus.SKIPPED
        row.error = 'story not found'
        row.save(update_fields=['status', 'error', 'dt_updated'])
        return False

    story_summary = getattr(story, 'summary', None) or ''
    # 正文要给两条路径都用: LLM 路径要它做摘要, 降级路径要用它估算价值分。
    # 之前只在 config.is_enabled 时才读, 结果降级模式下打分只能拿到 RSS
    # 那几十个字的 summary, 全部文章都被打成最低分。
    # 一次 get_by_offset 走的是本地 volume 表, 单条开销可以接受。
    content = _load_content(story) or ''

    try:
        if not config.is_enabled:
            # 没配 Key: 退化成规则分类 + RSS 原文摘要, 保证功能不残废。
            # 注意这里仍然标记为 DONE —— 记录本身是"处理完成"的,
            # model=fallback 用来区分是否经过 LLM。若标记成 SKIPPED,
            # 信息流接口(只查 DONE)在无 Key 时会是空的。
            result = fallback_summary(
                title=story.title, summary=story_summary, content=content
            )
            if result is None:
                raise AIError('empty article, skip')
            status = AIStatus.DONE
        else:
            result = summarize_story(
                title=story.title,
                content=content,
                summary=story_summary,
                link=story.link,
            )
            status = AIStatus.DONE
    except Exception as ex:  # noqa: BLE001 - 任务层不能因为单个异常整体崩溃
        retry_count = row.retry_count + 1
        final_status = (
            AIStatus.SKIPPED
            if retry_count > config.max_task_retries
            else AIStatus.FAILED
        )
        StoryAI.objects.filter(id=row.id).update(
            status=final_status,
            retry_count=retry_count,
            error=str(ex)[:500],
            dt_updated=timezone.now(),
        )
        LOG.warning(
            'ai summarize failed story=%s retry=%s status=%s: %s',
            row.story_id, retry_count, final_status, ex,
        )
        return False

    StoryAI.objects.filter(id=row.id).update(
        ai_summary=result.summary,
        ai_points=result.points,
        ai_keywords=result.keywords,
        topic=result.topic or DEFAULT_TOPIC,
        ai_score=result.score,
        status=status,
        model=result.model,
        error='',
        dt_processed=timezone.now(),
        dt_updated=timezone.now(),
    )
    return True


def reset_stale_processing() -> int:
    """把卡在 PROCESSING 的僵尸记录放回队列。"""
    deadline = timezone.now() - timedelta(minutes=STALE_PROCESSING_MINUTES)
    return StoryAI.objects.filter(
        status=AIStatus.PROCESSING, dt_updated__lt=deadline
    ).update(status=AIStatus.PENDING, dt_updated=timezone.now())


def sweep_pending(limit: int = None) -> dict:
    """巡查并处理待办/失败记录。带硬上限, 绝不会无界扩充。

    选记录的规则刻意分成两类:
        * failed  : 立刻重试(它已经失败过了, 不存在"正在处理"的可能)
        * pending : 必须等过宽限期 grace 秒。因为这些记录很可能已经被
          on_new_stories / refresh 派发进队列，只是还没轮到执行；
          巡查再抓一遍就变成同一篇被两个执行者争抢。
          (争抢本身由 claim_story_ai 挡住，但白跑一趟也是浪费。)
    """
    config = get_config()
    limit = limit or config.batch_size
    reset_stale_processing()

    grace_deadline = timezone.now() - timedelta(
        seconds=max(0, config.sweep_pending_grace)
    )
    queryset = (
        StoryAI.objects.filter(
            Q(status=AIStatus.FAILED)
            | Q(status=AIStatus.PENDING, dt_updated__lt=grace_deadline)
        )
        .filter(retry_count__lte=config.max_task_retries)
        .order_by('dt_created')
        .values_list('id', flat=True)[:limit]
    )
    ai_ids = list(queryset)
    ok = 0
    failed = 0
    for ai_id in ai_ids:
        if process_story_ai(ai_id):
            ok += 1
        else:
            failed += 1
    if ai_ids:
        LOG.info('ai sweep done total=%s ok=%s failed=%s', len(ai_ids), ok, failed)
    return {'total': len(ai_ids), 'ok': ok, 'failed': failed}


# ---------------------------------------------------------------------------
# 每日 AI 简报
# ---------------------------------------------------------------------------
def _candidate_of(row: StoryAI) -> dict:
    story = row.story
    return {
        'id': row.story_id,
        'title': story.title,
        'summary': row.ai_summary or story.summary or '',
        'topic': row.topic,
        'keywords': row.ai_keywords or [],
        'ai_score': row.ai_score,
    }


def collect_recent_stories(hours: int = 24, limit: int = 30) -> List[StoryAI]:
    since = timezone.now() - timedelta(hours=hours)
    queryset = (
        StoryAI.objects.filter(status=AIStatus.DONE, dt_created__gte=since)
        .select_related('story')
        .order_by('-ai_score', '-dt_created')[:limit]
    )
    return list(queryset)


def build_daily_digest(
    date=None, hours: int = 24, candidate_limit: int = 30, item_limit: int = 8
) -> DailyDigest:
    """生成(或覆盖)某一天的 AI 资讯简报。"""
    config = get_config()
    target_date = date or timezone.localdate()
    rows = collect_recent_stories(hours=hours, limit=candidate_limit)
    candidates = [_candidate_of(x) for x in rows]
    link_map = {x.story_id: (x.story.link or '') for x in rows}

    try:
        if not candidates:
            result = ai_digest.DailyDigestResult(
                overview='今日暂无新增文章。', items=[], model='none'
            )
        elif not config.is_enabled:
            # 没配 Key: 退化为按评分排序的规则简报
            result = ai_digest.DailyDigestResult(
                overview='未配置 AI，以下按文章评分自动生成。',
                items=[
                    ai_digest.DigestItem(
                        story_id=x['id'],
                        title=x['title'],
                        digest=x['summary'][:120],
                        reason=f"命中主题 {x['topic']}",
                        score=x['ai_score'],
                    )
                    for x in candidates[:item_limit]
                ],
                model='fallback',
            )
        else:
            result = ai_digest.build_daily_digest(candidates, limit=item_limit)
    except Exception as ex:  # noqa: BLE001
        LOG.error('build daily digest failed: %s', ex)
        digest, _ = DailyDigest.objects.update_or_create(
            date=target_date,
            defaults={
                'status': AIStatus.FAILED,
                'error': str(ex)[:500],
                'story_count': len(candidates),
            },
        )
        return digest

    markdown = ai_digest.render_markdown(target_date.isoformat(), result)

    # 补上原文链接, 方便前端直接跳转
    items = []
    for item in result.items:
        data = item.to_dict()
        data['link'] = link_map.get(item.story_id, '')
        items.append(data)

    digest, _ = DailyDigest.objects.update_or_create(
        date=target_date,
        defaults={
            'overview': result.overview,
            'items': items,
            'markdown': markdown,
            'story_count': len(candidates),
            'model': result.model,
            'status': AIStatus.DONE,
            'error': '',
        },
    )
    LOG.info(
        'daily digest %s built items=%s candidates=%s',
        target_date, len(items), len(candidates),
    )
    return digest


# ---------------------------------------------------------------------------
# 用户兴趣
# ---------------------------------------------------------------------------
def get_interest(user) -> UserInterest:
    row = UserInterest.objects.filter(user_id=user.id).first()
    if row is None:
        row, _ = UserInterest.objects.get_or_create(user_id=user.id)
    return row


def set_interest(
    user,
    keywords: str = None,
    topics: list = None,
    threshold: float = None,
    enable: bool = None,
) -> UserInterest:
    row = get_interest(user)
    if keywords is not None:
        row.keywords = keywords.strip()
    if topics is not None:
        row.topics = [x for x in topics if x in TOPICS]
    if threshold is not None:
        row.threshold = float(threshold)
    if enable is not None:
        row.enable = bool(enable)
    row.save()
    return row


def interest_match_score(interest: UserInterest, story_row: StoryAI) -> float:
    """计算一篇文章与用户兴趣的匹配度。未启用兴趣过滤时恒为 0。"""
    if interest is None or not interest.enable:
        return 0.0
    return ai_interest.match_score(
        title=story_row.story.title if story_row.story else '',
        summary=story_row.ai_summary or '',
        keywords=story_row.ai_keywords or [],
        interest_keywords=ai_interest.parse_keywords(interest.keywords),
        interest_topics=interest.topics or [],
        topic=story_row.topic,
    )


def cleanup_old_ai_data(days: int = 90) -> dict:
    """清理过期的 AI 数据, 控制表体积(低配机器上很重要)。"""
    deadline = timezone.now() - timedelta(days=days)
    digest_deleted, _ = DailyDigest.objects.filter(date__lt=deadline.date()).delete()
    return {'digest_deleted': digest_deleted}
