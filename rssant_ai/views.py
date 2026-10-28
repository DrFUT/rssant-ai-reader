"""AI 能力对外 HTTP 接口。

接口风格沿用 rssant 的 django_rest_validr.RestRouter:
    * 权限沿用全局配置(默认要求登录)
    * 输出统一用 rest_framework.Response, 避免再套一层 schema 校验
      (AI 结果字段本身是可选的, 强制 schema 反而容易 500)
"""

import logging

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render

from django_rest_validr import RestRouter, T
from rest_framework.response import Response

from ai import interest as ai_interest
from ai.config import get_config
from ai.prompts import TOPICS

from rssant_api.models import UserFeed

from .models import AIStatus, DailyDigest, StoryAI, UserInterest
from .service import build_daily_digest, get_interest, set_interest

LOG = logging.getLogger(__name__)


def _dispatch_summarize(ai_id: int) -> None:
    """延迟导入: 避免 URL 加载阶段就把 Celery app 拉起来。"""
    from .tasks import summarize_story_task

    summarize_story_task.delay(ai_id)


def _dispatch_digest(hours: int, item_limit: int) -> None:
    from .tasks import generate_daily_digest

    generate_daily_digest.delay(hours=hours, item_limit=item_limit)

AIView = RestRouter()

MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _int_param(request, name, default, low=0, high=MAX_LIMIT):
    raw = request.query_params.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _user_feed_ids(user):
    return list(
        UserFeed.objects.filter(user=user)
        .exclude(feed__isnull=True)
        .values_list('feed_id', flat=True)
    )


def _feed_ids_q(feed_ids):
    """把「属于这些订阅」表达成 StoryAI.story_id 的主键区间。

    StoryInfo 的主键就是 StoryId.encode(feed_id, offset)
    = (feed_id << 32) + (offset << 4) + reserve，没有独立的 feed_id 列，
    所以只能用主键区间过滤。好处是直接命中主键索引，不用 join 文章表。
    """
    q = Q()
    for feed_id in feed_ids:
        base = int(feed_id) << 32
        q |= Q(story_id__gte=base, story_id__lt=base + (1 << 32))
    return q or Q(pk__in=[])


def _story_dict(row: StoryAI, interest: UserInterest = None, with_content: bool = False) -> dict:
    story = row.story
    data = {
        'id': row.story_id,
        'feed_id': story.feed_id if story else None,
        'title': story.title if story else '',
        'link': story.link if story else '',
        'author': story.author if story else '',
        'image_url': story.image_url if story else '',
        'dt_published': story.dt_published if story else None,
        'dt_created': story.dt_created if story else None,
        'origin_summary': story.summary if story else '',
        'ai_summary': row.ai_summary,
        'ai_points': row.ai_points or [],
        'ai_keywords': row.ai_keywords or [],
        'topic': row.topic,
        'ai_score': row.ai_score,
        'ai_status': row.status,
        'dt_processed': row.dt_processed,
    }
    if interest is not None and interest.enable:
        data['interest_score'] = ai_interest.match_score(
            title=data['title'],
            summary=row.ai_summary or data['origin_summary'],
            keywords=row.ai_keywords or [],
            interest_keywords=ai_interest.parse_keywords(interest.keywords),
            interest_topics=interest.topics or [],
            topic=row.topic,
        )
    else:
        data['interest_score'] = 0.0
    if with_content:
        data['origin_summary'] = data['origin_summary'] or ''
    return data


# ---------------------------------------------------------------------------
# 运行状态
# ---------------------------------------------------------------------------
@AIView.get('ai/status')
def ai_status(request):
    """AI 功能运行状态: 是否启用、模型、队列积压情况。"""
    config = get_config()
    counts = {
        x['status']: x['count']
        for x in StoryAI.objects.values('status').annotate(count=Count('id'))
    }
    return Response(
        {
            'enabled': config.is_enabled,
            'model': config.model,
            'base_url': config.base_url,
            'timeout': config.timeout,
            'batch_size': config.batch_size,
            'topics': TOPICS,
            'queue': {
                'pending': counts.get(AIStatus.PENDING, 0),
                'processing': counts.get(AIStatus.PROCESSING, 0),
                'failed': counts.get(AIStatus.FAILED, 0),
                'done': counts.get(AIStatus.DONE, 0),
                'skipped': counts.get(AIStatus.SKIPPED, 0),
            },
        }
    )


# ---------------------------------------------------------------------------
# 文章流
# ---------------------------------------------------------------------------
@AIView.get('ai/stories')
def ai_stories(request):
    """带 AI 摘要的信息流。

    查询参数:
        topic    主题筛选
        q        标题/摘要关键词
        min_score 最低评分
        interest 1 表示只返回命中用户兴趣的文章
        sort     time(默认) | score | interest
        offset / limit
    """
    feed_ids = _user_feed_ids(request.user)
    if not feed_ids:
        return Response({'total': 0, 'stories': []})

    topic = (request.query_params.get('topic') or '').strip()
    keyword = (request.query_params.get('q') or '').strip()
    min_score = _int_param(request, 'min_score', 0, 0, 10)
    only_interest = request.query_params.get('interest') in ('1', 'true', 'True')
    sort = (request.query_params.get('sort') or 'time').strip()
    offset = _int_param(request, 'offset', 0, 0, 100000)
    limit = _int_param(request, 'limit', DEFAULT_LIMIT, 1, MAX_LIMIT)

    queryset = StoryAI.objects.filter(status=AIStatus.DONE).filter(
        _feed_ids_q(feed_ids)
    )
    if topic:
        queryset = queryset.filter(topic=topic)
    if keyword:
        queryset = queryset.filter(
            Q(story__title__icontains=keyword) | Q(ai_summary__icontains=keyword)
        )
    if min_score:
        queryset = queryset.filter(ai_score__gte=min_score)

    if sort == 'score':
        queryset = queryset.order_by('-ai_score', '-story__dt_created')
    else:
        queryset = queryset.order_by('-story__dt_created')

    # 兴趣过滤在 Python 侧做: 命中逻辑是加权规则, 放进 SQL 反而不可读且难调
    interest = get_interest(request.user)
    if sort == 'interest' or only_interest:
        candidates = list(queryset.select_related('story')[: MAX_LIMIT * 5])
        items = [_story_dict(x, interest) for x in candidates]
        if only_interest:
            items = [x for x in items if x['interest_score'] >= interest.threshold]
        items.sort(key=lambda x: (-x['interest_score'], -x['ai_score']))
        total = len(items)
        stories = items[offset: offset + limit]
    else:
        total = queryset.count()
        stories = [
            _story_dict(x, interest)
            for x in queryset.select_related('story')[offset: offset + limit]
        ]

    return Response({'total': total, 'offset': offset, 'limit': limit, 'stories': stories})


@AIView.get('ai/story/<int:story_id>')
def ai_story_detail(request, story_id: T.int):
    """单篇文章的 AI 结果。"""
    row = StoryAI.objects.filter(story_id=story_id).select_related('story').first()
    if row is None:
        return Response({'detail': 'not found'}, status=404)
    return Response(_story_dict(row, get_interest(request.user)))


@AIView.post('ai/story/<int:story_id>/refresh')
def ai_story_refresh(request, story_id: T.int):
    """手动重新生成某篇文章的 AI 摘要(立即派发异步任务)。"""
    row = StoryAI.objects.filter(story_id=story_id).first()
    if row is None:
        from .service import ensure_story_ai

        new_ids = ensure_story_ai([story_id])
        if not new_ids:
            return Response({'detail': 'not found'}, status=404)
        row = StoryAI.objects.get(id=new_ids[0])
    StoryAI.objects.filter(id=row.id).update(
        status=AIStatus.PENDING, retry_count=0, error=''
    )
    _dispatch_summarize(row.id)
    return Response({'ok': True, 'ai_id': row.id, 'status': AIStatus.PENDING})


# ---------------------------------------------------------------------------
# 每日简报
# ---------------------------------------------------------------------------
@AIView.get('ai/digest/latest')
def ai_digest_latest(request):
    digest = DailyDigest.objects.filter(status=AIStatus.DONE).order_by('-date').first()
    if digest is None:
        return Response({'detail': 'no digest yet'}, status=404)
    include_markdown = request.query_params.get('markdown') in ('1', 'true', 'True')
    return Response(digest.to_dict(include_markdown=include_markdown))


@AIView.get('ai/digest/<str:date>')
def ai_digest_by_date(request, date: T.str):
    digest = DailyDigest.objects.filter(date=date).first()
    if digest is None:
        return Response({'detail': 'not found'}, status=404)
    include_markdown = request.query_params.get('markdown') in ('1', 'true', 'True')
    return Response(digest.to_dict(include_markdown=include_markdown))


@AIView.post('ai/digest/generate')
def ai_digest_generate(request):
    """手动触发生成今日简报。

    两种模式:
        async=1(默认) 派发 Celery 任务
        async=0        同步生成, 方便演示和排障
    """
    is_async = str(request.data.get('async', '1')) not in ('0', 'false', 'False')
    hours = int(request.data.get('hours') or 24)
    item_limit = int(request.data.get('item_limit') or 8)
    if is_async:
        _dispatch_digest(hours=hours, item_limit=item_limit)
        return Response({'ok': True, 'mode': 'async'})
    digest = build_daily_digest(hours=hours, item_limit=item_limit)
    return Response(digest.to_dict(include_markdown=True))


# ---------------------------------------------------------------------------
# 兴趣配置
# ---------------------------------------------------------------------------
@AIView.get('ai/interest')
def ai_interest_get(request):
    row = get_interest(request.user)
    return Response(row.to_dict())


@AIView.post('ai/interest')
def ai_interest_set(request):
    data = request.data or {}
    keywords = data.get('keywords')
    if isinstance(keywords, list):
        keywords = ','.join(str(x) for x in keywords)
    topics = data.get('topics') or []
    if isinstance(topics, str):
        topics = [x.strip() for x in topics.split(',') if x.strip()]
    threshold = data.get('threshold')
    threshold = float(threshold) if threshold not in (None, '') else None
    enable = data.get('enable')
    enable = bool(enable) if enable is not None else None
    row = set_interest(
        request.user,
        keywords=keywords,
        topics=[x for x in topics if x in TOPICS],
        threshold=threshold,
        enable=enable,
    )
    return Response(row.to_dict())


@AIView.get('ai/topics')
def ai_topics(request):
    """各主题的文章数量, 前端用于筛选器。"""
    feed_ids = _user_feed_ids(request.user)
    queryset = StoryAI.objects.filter(status=AIStatus.DONE)
    if feed_ids:
        queryset = queryset.filter(story__feed_id__in=feed_ids)
    counts = {x['topic']: x['count'] for x in queryset.values('topic').annotate(count=Count('id'))}
    return Response(
        {'topics': [{'topic': t, 'count': counts.get(t, 0)} for t in TOPICS]}
    )


# ---------------------------------------------------------------------------
# 页面(轻量前端)
# ---------------------------------------------------------------------------
@login_required
def reader_page(request):
    """AI 信息流阅读器页面。

    rssant 官方前端未开源(box/web 不在仓库中), 这里提供一个零构建步骤的
    轻量页面, 直接调用上面的 /api/v1/ai/* 接口。
    """
    config = get_config()
    return render(
        request,
        'rssant_ai/reader.html',
        {
            'ai_enabled': config.is_enabled,
            'ai_model': config.model,
            'topics': TOPICS,
            'api_base': '/api/v1/ai',
        },
    )
