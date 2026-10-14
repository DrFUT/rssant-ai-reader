"""OpenAI 兼容的 Chat Completions 客户端。

为什么不直接用官方 SDK:
    * 官方 SDK 依赖较重，而本项目已经依赖 requests
    * 需要精细控制超时/重试/连接复用，直接写更可控
    * DeepSeek / Moonshot / 通义 / 本地 OpenAI 兼容网关都是同一套协议，
      换服务商只需要改 .env 里的 AI_BASE_URL 和 AI_MODEL

只依赖标准库 + requests。
"""

import logging
import random
import time
from typing import Any, Dict, List, Optional

import requests

from .config import AIConfig, get_config
from .errors import AIConfigError, AIRequestError, AIRateLimitError

LOG = logging.getLogger(__name__)

# 遇到这些状态码才重试；4xx(除 429)属于请求本身有问题，重试没意义
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMClient:
    """极简 LLM 客户端。"""

    def __init__(self, config: AIConfig = None, session: requests.Session = None):
        self.config = config or get_config()
        self._session = session
        if not self.config.api_key:
            raise AIConfigError(
                'AI_API_KEY is not configured, please set it in .env file'
            )

    @property
    def session(self) -> requests.Session:
        # 复用连接，避免每次调用都重新握手(低配机器上这点开销很明显)
        if self._session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=2, pool_maxsize=4, max_retries=0
            )
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self._session = session
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            'Authorization': f'Bearer {self.config.api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = None,
        max_tokens: int = None,
        timeout: int = None,
        max_retries: int = None,
        response_format: dict = None,
    ) -> str:
        """调用 chat/completions，返回 assistant 的文本内容。

        失败时抛出 AIRequestError / AIRateLimitError。
        """
        config = self.config
        payload: Dict[str, Any] = {
            'model': config.model,
            'messages': messages,
            'temperature': config.temperature if temperature is None else temperature,
            'max_tokens': config.max_output_tokens if max_tokens is None else max_tokens,
            'stream': False,
        }
        if response_format:
            payload['response_format'] = response_format

        url = config.chat_completions_url
        timeout = config.timeout if timeout is None else timeout
        max_retries = config.max_retries if max_retries is None else max_retries

        last_error = None
        for attempt in range(max_retries + 1):
            started = time.time()
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=timeout,
                )
            except requests.exceptions.Timeout as ex:
                last_error = f'timeout after {timeout}s'
                LOG.warning('ai request %s timeout (attempt %s)', url, attempt)
            except requests.exceptions.RequestException as ex:  # noqa: B014
                last_error = f'{type(ex).__name__}: {ex}'
                LOG.warning('ai request %s error (attempt %s): %s', url, attempt, ex)
            else:
                cost = int((time.time() - started) * 1000)
                if response.status_code == 200:
                    try:
                        return self._extract_content(response.json())
                    except ValueError as ex:
                        last_error = f'invalid json response: {ex}'
                        LOG.warning('ai invalid json response: %s', ex)
                else:
                    body = (response.text or '')[:500]
                    last_error = f'status={response.status_code} body={body}'
                    LOG.warning(
                        'ai request %s status=%s cost=%dms body=%s',
                        url, response.status_code, cost, body,
                    )
                    if response.status_code == 429:
                        # 限流：退避后重试，最后一次直接抛限流异常让任务层跳过
                        if attempt >= max_retries:
                            raise AIRateLimitError(
                                f'ai rate limited: {last_error}',
                                status_code=429,
                                body=body,
                            )
                    elif response.status_code not in _RETRYABLE_STATUS:
                        raise AIRequestError(
                            f'ai request failed: {last_error}',
                            status_code=response.status_code,
                            body=body,
                        )
            if attempt < max_retries:
                # 指数退避 + 抖动，避免多个任务同时重试造成雪崩
                delay = min(2 ** attempt, 8) + random.uniform(0, 0.5)
                time.sleep(delay)

        raise AIRequestError(f'ai request failed after retries: {last_error}')

    @staticmethod
    def _extract_content(data: Any) -> str:
        if not isinstance(data, dict):
            raise ValueError(f'unexpected response type: {type(data)}')
        if data.get('error'):
            raise ValueError(f"llm error: {data['error']}")
        choices = data.get('choices') or []
        if not choices:
            raise ValueError(f'empty choices: {str(data)[:200]}')
        message = choices[0].get('message') or {}
        content = message.get('content')
        if content is None:
            content = choices[0].get('text')
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f'empty content: {str(choices[0])[:200]}')
        return content.strip()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


_CLIENT = None


def get_client(config: AIConfig = None) -> LLMClient:
    """进程内共享一个客户端(复用连接池)。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient(config=config)
    return _CLIENT


def reset_client() -> None:
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None
