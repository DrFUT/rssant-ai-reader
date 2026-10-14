"""从 LLM 输出里稳健地提取 JSON。

LLM 经常会裹一层 ```json 代码块，或者在 JSON 前后加解释性文字。
这里用尽量简单可靠的方式处理，而不是引入一个 JSON 修复库。
"""

import json
import re
from typing import Any, Optional

from .errors import AIResponseError

_FENCE_RE = re.compile(r'```(?:json|JSON)?\s*(.*?)```', re.DOTALL)
_JSON_OBJ_RE = re.compile(r'\{.*\}', re.DOTALL)
_JSON_ARR_RE = re.compile(r'\[.*\]', re.DOTALL)


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def loads(text: str, expect: str = 'object') -> Any:
    """从 LLM 文本中解析 JSON。

    expect: 'object' 或 'array'
    """
    if text is None:
        raise AIResponseError('empty llm output')
    raw = _strip_fence(text)
    candidates = [raw]
    pattern = _JSON_OBJ_RE if expect == 'object' else _JSON_ARR_RE
    match = pattern.search(raw)
    if match:
        candidates.append(match.group(0))
    last_error = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError) as ex:
            last_error = ex
    raise AIResponseError(f'can not parse llm output as json: {last_error}; raw={raw[:200]!r}')


def to_str_list(value: Any, limit: int = 8, max_len: int = 40) -> list:
    """把任意值规范成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r'[,，、;；|/]\s*', value)
    if not isinstance(value, (list, tuple)):
        value = [value]
    result = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        if len(text) > max_len:
            text = text[:max_len]
        if text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def to_int(value: Any, default: int, low: int = 0, high: int = 100) -> int:
    """把任意值规范成分区间的整数。"""
    if value is None:
        return default
    try:
        if isinstance(value, bool):
            return default
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def to_text(value: Any, max_len: int = 1000) -> str:
    if value is None:
        return ''
    text = str(value).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def first_of(data: dict, *keys, default=None) -> Optional[Any]:
    """LLM 经常把同一个字段换名字返回，这里做一次兜底。"""
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ''):
            return data.get(key)
    return default
