"""
ai - AI 服务层

一个轻量的、与 Web 框架无关的 LLM 能力封装，只依赖标准库 + requests。

设计目标(面向 2 核 2GB 的低配自托管环境):
    * 零额外重量级依赖，不引入官方 SDK，直接调用 OpenAI 兼容的 HTTP 接口
    * 所有网络调用都有超时 + 有限重试，绝不无限挂起
    * 摘要与分类合并为一次 LLM 调用，分类结果由本地规则归一化，节省 token
    * 任何异常都被收敛为 AIError，调用方(任务层)可安全降级

对外主要入口:
    ai.summarizer.summarize_story(...)   -> 摘要 + 关键观点 + 标签 + 评分
    ai.classifier.classify(...)          -> 主题 topic + 标签 tags
    ai.digest.build_daily_digest(...)    -> 每日资讯简报
    ai.interest.match_score(...)         -> 用户兴趣匹配度
"""

from .errors import AIError, AIConfigError, AIRateLimitError  # noqa: F401
from .client import LLMClient, get_client  # noqa: F401
from .config import get_config, is_enabled  # noqa: F401

__all__ = [
    'AIError',
    'AIConfigError',
    'AIRateLimitError',
    'LLMClient',
    'get_client',
    'get_config',
    'is_enabled',
]

__version__ = '1.0.0'
