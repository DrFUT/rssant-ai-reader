"""正文清洗与截断。

rssant 存的文章正文是 HTML，直接丢给 LLM 会浪费大量 token。
这里做一次轻量清洗(纯标准库，不引入 html2text/lxml)。
"""

import html
import re
from functools import lru_cache
from typing import Optional

_CJK_RE = re.compile(r'[\u4e00-\u9fff]')


@lru_cache(maxsize=512)
def _pattern_for(keyword: str):
    """为关键词构造匹配模式。

    纯 ASCII 关键词(如 ai / go / rust)必须按单词边界匹配,
    否则 "ai" 会命中 "trait"、"go" 会命中 "django"。
    含中文的关键词直接做子串匹配。
    """
    if not keyword:
        return None
    if _CJK_RE.search(keyword):
        return re.compile(re.escape(keyword.lower()))
    return re.compile(r'\b' + re.escape(keyword.lower()) + r'\b')


def contains(haystack: str, keyword: str) -> bool:
    """关键词匹配: 中文子串, 英文按单词边界。"""
    if not haystack or not keyword:
        return False
    pattern = _pattern_for(keyword.strip().lower())
    if pattern is None:
        return False
    return bool(pattern.search(haystack.lower()))

_SCRIPT_RE = re.compile(
    r'<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r'<[^>]+>')
_BLOCK_RE = re.compile(r'(?m)^\s*(?:<br\s*/?>|</(?:p|div|li|h\d)>)\s*$')
_WS_RE = re.compile(r'[ \t\u00a0]+')
_BLANK_RE = re.compile(r'\n\s*\n\s*\n+')


def strip_html(text: Optional[str]) -> str:
    """把 HTML 压成纯文本。"""
    if not text:
        return ''
    raw = _SCRIPT_RE.sub(' ', text)
    raw = _BLOCK_RE.sub('\n', raw)
    raw = _TAG_RE.sub('\n', raw)
    raw = html.unescape(raw)
    raw = _WS_RE.sub(' ', raw)
    raw = _BLANK_RE.sub('\n\n', raw)
    lines = [line.strip() for line in raw.split('\n')]
    return '\n'.join(line for line in lines if line).strip()


def truncate(text: Optional[str], max_chars: int) -> str:
    """按字符数截断，尽量在句子边界断开。"""
    if not text:
        return ''
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for sep in ('。', '. ', '！', '？', '\n', '；'):
        index = head.rfind(sep)
        if index > max_chars * 0.6:
            return head[: index + 1].strip()
    return head.strip()


def build_article_text(
    title: str = None,
    summary: str = None,
    content: str = None,
    link: str = None,
    max_chars: int = 6000,
) -> str:
    """组织喂给 LLM 的文章文本。

    正文优先；没有正文时退化为 RSS 自带的 summary。
    """
    body = strip_html(content) if content else ''
    if len(body) < 200:
        fallback = strip_html(summary)
        if len(fallback) > len(body):
            body = fallback
    body = truncate(body, max_chars)
    parts = []
    if title:
        parts.append(f'标题: {strip_html(title)}')
    if link:
        parts.append(f'链接: {link}')
    if body:
        parts.append(f'正文:\n{body}')
    return '\n'.join(parts).strip()
