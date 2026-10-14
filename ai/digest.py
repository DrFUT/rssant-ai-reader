"""每日 AI 资讯简报。

流程(由 Celery Beat 每天定时触发):
    读取最近 24 小时的高价值文章
        -> 压缩成候选列表(只带标题/摘要/标签, 不传正文, 控制 token)
        -> 一次 LLM 调用产出 今日概览 + 精选条目(含推荐原因)
        -> 结构化落库
"""

import json
import logging
from typing import Any, Dict, List, Optional

from .client import LLMClient, get_client
from .config import get_config
from .errors import AIError
from . import json_helper as J
from .prompts import SYSTEM_DIGEST, USER_DIGEST
from .text import truncate

LOG = logging.getLogger(__name__)

# 单篇候选在 prompt 里允许占用的字符数
_CANDIDATE_CHARS = 320


class DigestItem:
    __slots__ = ('story_id', 'title', 'digest', 'reason', 'score')

    def __init__(
        self,
        story_id: int,
        title: str = '',
        digest: str = '',
        reason: str = '',
        score: int = 0,
    ):
        self.story_id = story_id
        self.title = title
        self.digest = digest
        self.reason = reason
        self.score = score

    def to_dict(self) -> dict:
        return {
            'story_id': self.story_id,
            'title': self.title,
            'digest': self.digest,
            'reason': self.reason,
            'score': self.score,
        }


class DailyDigestResult:
    __slots__ = ('overview', 'items', 'model', 'raw')

    def __init__(
        self,
        overview: str = '',
        items: List[DigestItem] = None,
        model: str = '',
        raw: str = '',
    ):
        self.overview = overview
        self.items = items or []
        self.model = model
        self.raw = raw

    def to_dict(self) -> dict:
        return {
            'overview': self.overview,
            'items': [x.to_dict() for x in self.items],
            'model': self.model,
        }


def _candidate(story: Dict[str, Any]) -> dict:
    """把一篇文章压成候选条目, 尽量短。"""
    keywords = story.get('keywords') or []
    if isinstance(keywords, str):
        keywords = [x for x in keywords.split(',') if x]
    return {
        'id': story.get('id'),
        'title': truncate(story.get('title') or '', 120),
        'topic': story.get('topic') or '',
        'keywords': list(keywords)[:5],
        'summary': truncate(story.get('summary') or '', _CANDIDATE_CHARS),
        'ai_score': story.get('ai_score') or 0,
    }


def build_prompt(stories: List[Dict[str, Any]], limit: int) -> List[dict]:
    candidates = [_candidate(x) for x in stories]
    payload = json.dumps(candidates, ensure_ascii=False)
    return [
        {'role': 'system', 'content': SYSTEM_DIGEST},
        {'role': 'user', 'content': USER_DIGEST.format(articles=payload, limit=limit)},
    ]


def build_daily_digest(
    stories: List[Dict[str, Any]],
    limit: int = 8,
    client: LLMClient = None,
) -> DailyDigestResult:
    """生成每日简报。stories 为空时直接返回空结果, 不浪费 LLM 调用。"""
    config = get_config()
    if not stories:
        return DailyDigestResult(overview='今日暂无新增文章。', items=[], model='none')

    if not client:
        client = get_client()

    raw = client.chat(build_prompt(stories, limit))
    data = J.loads(raw, expect='object')

    valid_ids = {x.get('id') for x in stories}
    items = []
    raw_items = data.get('items') or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        story_id = row.get('story_id', row.get('id'))
        try:
            story_id = int(story_id)
        except (TypeError, ValueError):
            continue
        # 过滤掉模型编造的 id
        if story_id not in valid_ids:
            LOG.warning('digest item story_id=%s not in candidates, skip', story_id)
            continue
        items.append(
            DigestItem(
                story_id=story_id,
                title=J.to_text(row.get('title'), max_len=300),
                digest=J.to_text(row.get('digest'), max_len=800),
                reason=J.to_text(row.get('reason'), max_len=300),
                score=J.to_int(row.get('score'), default=5),
            )
        )
        if len(items) >= limit:
            break

    overview = J.to_text(data.get('overview') or data.get('summary'), max_len=600)
    return DailyDigestResult(
        overview=overview, items=items, model=config.model, raw=raw[:4000]
    )


def render_markdown(date_text: str, result: DailyDigestResult) -> str:
    """把简报渲染成 Markdown, 便于直接展示或导出。"""
    lines = [f'# AI 资讯简报 {date_text}', '']
    if result.overview:
        lines.append(f'> {result.overview}')
        lines.append('')
    if not result.items:
        lines.append('今日没有值得推荐的文章。')
        return '\n'.join(lines)
    lines.append('## 今日热点')
    lines.append('')
    for index, item in enumerate(result.items, start=1):
        lines.append(f'### {index}. {item.title}')
        lines.append('')
        if item.digest:
            lines.append(item.digest)
            lines.append('')
        if item.reason:
            lines.append(f'- 推荐原因: {item.reason}')
        lines.append(f'- 推荐指数: {item.score}/10')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'
