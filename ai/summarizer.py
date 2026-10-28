"""文章摘要。

一次 LLM 调用同时产出:
    - 一句话总结
    - 3 条关键观点
    - 关键词/标签
    - 一级主题
    - 价值评分(1-10)
"""

import logging
from typing import List, Optional

from .classifier import classify
from .client import LLMClient, get_client
from .config import get_config
from .errors import AIError
from . import json_helper as J
from .prompts import SYSTEM_SUMMARY, TOPICS, USER_SUMMARY
from .text import build_article_text

LOG = logging.getLogger(__name__)


class StorySummary:
    """一篇结构化摘要结果。"""

    __slots__ = (
        'summary', 'points', 'keywords', 'topic', 'score', 'model', 'raw'
    )

    def __init__(
        self,
        summary: str = '',
        points: List[str] = None,
        keywords: List[str] = None,
        topic: str = '',
        score: int = 0,
        model: str = '',
        raw: str = '',
    ):
        self.summary = summary
        self.points = points or []
        self.keywords = keywords or []
        self.topic = topic
        self.score = score
        self.model = model
        self.raw = raw

    def to_dict(self) -> dict:
        return {
            'summary': self.summary,
            'points': self.points,
            'keywords': self.keywords,
            'topic': self.topic,
            'score': self.score,
            'model': self.model,
        }

    def __repr__(self) -> str:
        return f'StorySummary(topic={self.topic!r}, score={self.score}, summary={self.summary[:30]!r})'


def _build_messages(article: str) -> list:
    return [
        {'role': 'system', 'content': SYSTEM_SUMMARY},
        {
            'role': 'user',
            'content': USER_SUMMARY.format(
                article=article, topics='、'.join(TOPICS)
            ),
        },
    ]


def summarize_story(
    title: str = None,
    content: str = None,
    summary: str = None,
    link: str = None,
    client: LLMClient = None,
) -> StorySummary:
    """调用 LLM 生成结构化摘要。

    抛出 AIError 的子类时，调用方应做降级处理(例如标记为失败稍后重试)。
    """
    config = get_config()
    if not client:
        client = get_client()

    article = build_article_text(
        title=title,
        summary=summary,
        content=content,
        link=link,
        max_chars=config.max_input_chars,
    )
    if not article:
        raise AIError('empty article text, skip ai summarize')

    raw = client.chat(_build_messages(article))
    try:
        data = J.loads(raw, expect='object')
    except AIError:
        LOG.warning('ai summarize returns non-json output: %r', raw[:200])
        raise

    points = J.to_str_list(
        J.first_of(data, 'points', 'key_points', 'keyPoints'), limit=3, max_len=120
    )
    keywords = J.to_str_list(
        J.first_of(data, 'keywords', 'keywords_list', 'tags'), limit=6, max_len=20
    )
    score = J.to_int(J.first_of(data, 'score', 'ai_score', 'value'), default=5)
    summary_text = J.to_text(J.first_of(data, 'summary', 'abstract'), max_len=500)

    topic, tags = classify(
        topic=J.first_of(data, 'topic', 'category'),
        keywords=keywords,
        title=title,
        text=summary_text or summary,
    )

    return StorySummary(
        summary=summary_text,
        points=points,
        keywords=tags or keywords,
        topic=topic,
        score=score,
        model=config.model,
        raw=raw[:4000],
    )


def fallback_summary(
    title: str = None,
    summary: str = None,
    content: str = None,
) -> Optional[StorySummary]:
    """没有配置 AI 时的降级摘要: 直接用 RSS 自带摘要的前若干字。

    保证系统在没有 Key 的情况下依然可用(只是没有 AI 增强)。
    """
    text = (summary or content or '').strip()
    if not text:
        return None
    from .text import strip_html, truncate

    plain = truncate(strip_html(text), 120)
    topic, tags = classify(title=title, text=plain)
    return StorySummary(
        summary=plain,
        points=[],
        keywords=tags,
        topic=topic,
        score=_fallback_score(text),
        model='fallback',
    )


def _fallback_score(text: str) -> int:
    """降级模式下的"价值分"估算。

    没有 LLM 时不能凭空编一个精确分值, 但全给 0 会让按分数排序/筛选
    完全失效, 界面上也像是坏了。这里只用一个可解释的粗估:
    正文字数越多, 通常信息密度越高。

        < 200 字   -> 3
        200~800 字 -> 5
        > 800 字   -> 7
    """
    size = len(text)
    if size > 800:
        return 7
    if size > 200:
        return 5
    return 3
