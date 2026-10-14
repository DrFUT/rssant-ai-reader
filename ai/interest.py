"""用户兴趣过滤(轻量实现)。

刻意不做推荐算法，只做可解释的关键词加权匹配:

    match = 标题命中权重 + 摘要命中权重 + 标签命中权重

权重设计理由:
    标题命中权重最高 —— 标题通常最能代表文章主题
    标签命中次之    —— 标签是模型提炼过的语义信号
    摘要命中最低    —— 正文里偶然提及不应算强相关
"""

import re
from typing import Iterable, List

from .text import contains

TITLE_WEIGHT = 3.0
TAG_WEIGHT = 2.0
SUMMARY_WEIGHT = 1.0

# 命中一次的基础分
BASE_HIT = 1.0


def parse_keywords(raw) -> List[str]:
    """把用户配置解析成关键词列表，支持逗号/空格/换行分隔。"""
    if not raw:
        return []
    if isinstance(raw, str):
        parts = re.split(r'[,，、;；|\n\r]+', raw)
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = [str(raw)]
    result = []
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        if text.lower() not in [x.lower() for x in result]:
            result.append(text)
    return result


def _count_hits(haystack: str, keywords: Iterable[str]) -> int:
    if not haystack:
        return 0
    hits = 0
    for keyword in keywords:
        if not keyword:
            continue
        # contains() 对英文关键词按单词边界匹配, 避免 "AI" 命中 "trait"
        if contains(haystack, keyword):
            hits += 1
    return hits


def match_score(
    title: str = None,
    summary: str = None,
    keywords: Iterable[str] = None,
    interest_keywords: Iterable[str] = None,
    interest_topics: Iterable[str] = None,
    topic: str = None,
) -> float:
    """计算文章与用户兴趣的匹配度。

    返回 0 表示不相关；数值越大越相关。没有配置兴趣时恒返回 0(不做任何过滤)。
    """
    interests = parse_keywords(interest_keywords)
    topics = [str(x).strip() for x in (interest_topics or []) if str(x).strip()]
    if not interests and not topics:
        return 0.0

    score = 0.0
    if interests:
        score += BASE_HIT * TITLE_WEIGHT * _count_hits(title or '', interests)
        score += BASE_HIT * SUMMARY_WEIGHT * _count_hits(summary or '', interests)
        tag_text = ' '.join(str(x) for x in (keywords or []) if x)
        score += BASE_HIT * TAG_WEIGHT * _count_hits(tag_text, interests)
    if topics and topic:
        if str(topic) in topics:
            score += BASE_HIT * TAG_WEIGHT
    return round(score, 2)


def is_match(score: float, threshold: float = 1.0) -> bool:
    return score >= threshold
