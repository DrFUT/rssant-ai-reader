"""AI 配置。

配置优先级(从高到低):
    1. 带 RSSANT_ 前缀的环境变量(与 rssant 主工程的配置体系一致)
    2. 不带前缀的环境变量(便于非 compose 场景直接 export)
    3. .env 文件
    4. 代码内默认值

绝不硬编码 API Key。
"""

import os
from typing import Optional

DEFAULT_BASE_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-chat'

# 低配环境下的保守默认值
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 2


def _load_dotenv() -> None:
    """尽力加载 .env，失败就忽略(容器里通常由 docker compose 注入环境变量)。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    for name in ('RSSANT_CONFIG', 'RSSANT_ENV_FILE'):
        path = os.environ.get(name)
        if path:
            load_dotenv(path)
    load_dotenv('.env')


def _env(*names, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != '':
            return value.strip()
    return default


def _env_int(*names, default: int) -> int:
    raw = _env(*names, default=None)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(*names, default: float) -> float:
    raw = _env(*names, default=None)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(*names, default: bool) -> bool:
    raw = _env(*names, default=None)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


class AIConfig:
    """AI 服务的运行时配置。"""

    def __init__(self):
        _load_dotenv()
        self.api_key: str = _env('RSSANT_AI_API_KEY', 'AI_API_KEY', default='') or ''
        self.base_url: str = (
            _env('RSSANT_AI_BASE_URL', 'AI_BASE_URL', default=DEFAULT_BASE_URL)
            or DEFAULT_BASE_URL
        ).rstrip('/')
        self.model: str = (
            _env('RSSANT_AI_MODEL', 'AI_MODEL', default=DEFAULT_MODEL) or DEFAULT_MODEL
        )
        # 低配保护：单次请求超时 + 最大重试次数
        self.timeout: int = _env_int(
            'RSSANT_AI_TIMEOUT', 'AI_TIMEOUT', default=DEFAULT_TIMEOUT
        )
        self.max_retries: int = _env_int(
            'RSSANT_AI_MAX_RETRIES', 'AI_MAX_RETRIES', default=DEFAULT_MAX_RETRIES
        )
        self.temperature: float = _env_float(
            'RSSANT_AI_TEMPERATURE', 'AI_TEMPERATURE', default=0.3
        )
        self.max_output_tokens: int = _env_int(
            'RSSANT_AI_MAX_TOKENS', 'AI_MAX_TOKENS', default=800
        )
        # 送进 LLM 的正文最大字符数，避免大文章把 token 打爆
        self.max_input_chars: int = _env_int(
            'RSSANT_AI_MAX_INPUT_CHARS', 'AI_MAX_INPUT_CHARS', default=6000
        )
        # 每篇文章最多重试几次(由任务层使用)
        self.max_task_retries: int = _env_int(
            'RSSANT_AI_MAX_TASK_RETRIES', 'AI_MAX_TASK_RETRIES', default=2
        )
        # 单批最多处理多少篇，防止后台任务无限堆积
        self.batch_size: int = _env_int(
            'RSSANT_AI_BATCH_SIZE', 'AI_BATCH_SIZE', default=20
        )
        self.enable: bool = _env_bool('RSSANT_AI_ENABLE', 'AI_ENABLE', default=True)

    @property
    def is_enabled(self) -> bool:
        """只有显式开启且配置了 Key 才真正可用。"""
        return bool(self.enable and self.api_key)

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url
        # 常见的 OpenAI 兼容地址兼容处理
        if base.endswith('/v1'):
            return base + '/chat/completions'
        if base.endswith('/chat/completions'):
            return base
        return base + '/v1/chat/completions'

    def __repr__(self) -> str:
        masked = (self.api_key[:4] + '***') if len(self.api_key) > 8 else '***'
        return (
            f'AIConfig(model={self.model!r}, base_url={self.base_url!r}, '
            f'api_key={masked!r}, timeout={self.timeout}, '
            f'max_retries={self.max_retries}, enabled={self.is_enabled})'
        )


_CONFIG = None


def get_config() -> AIConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AIConfig()
    return _CONFIG


def reload_config() -> AIConfig:
    """测试或运行时改了环境变量后手动刷新。"""
    global _CONFIG
    _CONFIG = AIConfig()
    return _CONFIG


def is_enabled() -> bool:
    return get_config().is_enabled
