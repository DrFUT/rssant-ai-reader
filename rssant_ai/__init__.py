"""rssant_ai - 把 LLM 能力接入 rssant 阅读器的 Django 应用。

分层:
    ai/        纯 AI 服务层(不含 Django 依赖, 可单独测试)
    rssant_ai/ Django 集成层(模型 / Celery 任务 / HTTP 接口 / 页面)
"""

default_app_config = 'rssant_ai.apps.RssantAiConfig'
