"""Celery 任务定义。

任务本身都很"薄": 真正的逻辑在 service.py, 任务只负责
超时/重试/计数这些调度层面的事情。这样即便 Celery 出问题,
业务逻辑也可以被 manage.py shell 或测试直接调用。
"""

import logging

from ai.config import get_config

from .celery import app
from .service import build_daily_digest, cleanup_old_ai_data, sweep_pending

LOG = logging.getLogger(__name__)


@app.task(
    name='rssant_ai.tasks.summarize_story_task',
    bind=True,
    max_retries=0,
    soft_time_limit=90,
    time_limit=120,
)
def summarize_story_task(self, story_ai_id: int):
    """为单篇文章生成 AI 摘要。

    失败重试由 service 里的状态机控制(retry_count + status),
    这里不套 Celery 的重试, 避免失败任务在队列里反复堆积。
    """
    from .service import process_story_ai

    try:
        return process_story_ai(story_ai_id)
    except Exception as ex:  # noqa: BLE001
        LOG.exception('summarize_story_task failed ai_id=%s: %s', story_ai_id, ex)
        return False


@app.task(
    name='rssant_ai.tasks.sweep_pending_stories',
    bind=True,
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def sweep_pending_stories(self, limit: int = None):
    """巡查待处理/失败的文章摘要, 批量处理(带硬上限)。"""
    try:
        return sweep_pending(limit=limit)
    except Exception as ex:  # noqa: BLE001
        LOG.exception('sweep_pending_stories failed: %s', ex)
        return {'total': 0, 'ok': 0, 'failed': 0}


@app.task(
    name='rssant_ai.tasks.generate_daily_digest',
    bind=True,
    max_retries=0,
    soft_time_limit=300,
    time_limit=360,
)
def generate_daily_digest(self, hours: int = 24, item_limit: int = 8):
    """生成今日 AI 资讯简报。"""
    try:
        digest = build_daily_digest(hours=hours, item_limit=item_limit)
        return {'date': digest.date.isoformat(), 'items': len(digest.items or [])}
    except Exception as ex:  # noqa: BLE001
        LOG.exception('generate_daily_digest failed: %s', ex)
        return {'error': str(ex)}


@app.task(
    name='rssant_ai.tasks.cleanup_old_ai_data',
    bind=True,
    max_retries=0,
    soft_time_limit=120,
    time_limit=180,
)
def cleanup_old_ai_data_task(self, days: int = 90):
    try:
        return cleanup_old_ai_data(days=days)
    except Exception as ex:  # noqa: BLE001
        LOG.exception('cleanup_old_ai_data failed: %s', ex)
        return {'digest_deleted': 0}


@app.task(name='rssant_ai.tasks.ai_health_check', bind=True)
def ai_health_check(self):
    """轻量自检: 报告 AI 是否可用, 不真正消耗 token。"""
    config = get_config()
    return {
        'enabled': config.is_enabled,
        'model': config.model,
        'base_url': config.base_url,
        'timeout': config.timeout,
        'batch_size': config.batch_size,
    }


# 让 celery 在任务失败时不要尝试写入结果后端(省一点内存与连接)
app.conf.task_ignore_result = False
