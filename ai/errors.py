"""AI 模块异常定义。

所有对外抛出的异常都是 AIError 的子类，
调用方只需捕获 AIError 即可安全降级，不会因为底层 HTTP 库
抛出各种稀奇古怪的异常而把后台任务打挂。
"""


class AIError(Exception):
    """AI 能力的基础异常。"""


class AIConfigError(AIError):
    """配置缺失或非法，例如没有配置 AI_API_KEY。"""


class AIRequestError(AIError):
    """网络层失败或 LLM 返回了非 2xx 响应。"""

    def __init__(self, message: str, status_code: int = None, body: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AIRateLimitError(AIRequestError):
    """被限流(429)或服务端过载，重试也没有意义时需要快速失败。"""


class AIResponseError(AIError):
    """LLM 返回了内容，但无法解析成期望的 JSON 结构。"""
