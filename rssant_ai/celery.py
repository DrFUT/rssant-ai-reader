"""Celery 应用定义与定时任务表。

启动方式:
    celery -A rssant_ai worker -l info      # AI 异步任务消费者
    celery -A rssant_ai beat -l info        # 定时调度

面向 2 核 2GB 的关键约束(在 docker-compose 与 settings 中共同生效):
    * 单进程单并发(--concurrency=1), 避免抢占本来就紧张的 CPU
    * prefetch=1 + acks_late, 保证任务不堆积在 worker 内存里
    * 每个任务都有软/硬超时, LLM 卡住不会拖死整个 worker
    * max-tasks-per-child 限制, 防止长跑后内存缓慢膨胀
"""

import os

import django
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rssant.settings')
# Celery 任务里要用 ORM, 必须先把 Django 的 app 注册表准备好
django.setup()

app = Celery('rssant_ai')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


def _digest_hour() -> int:
    raw = os.getenv('RSSANT_AI_DIGEST_HOUR') or os.getenv('AI_DIGEST_HOUR') or '8'
    try:
        hour = int(raw)
    except (TypeError, ValueError):
        hour = 8
    return max(0, min(23, hour))


def _digest_minute() -> int:
    raw = os.getenv('RSSANT_AI_DIGEST_MINUTE') or os.getenv('AI_DIGEST_MINUTE') or '10'
    try:
        minute = int(raw)
    except (TypeError, ValueError):
        minute = 10
    return max(0, min(59, minute))


# ---------------------------------------------------------------------------
# 定时任务表
# ---------------------------------------------------------------------------
app.conf.beat_schedule = {
    # 新增/失败的文章由这个巡查任务兜底处理。
    # 之所以不把全部工作压在"入库即派发"上, 是因为订阅同步是批量写入,
    # 巡查任务天然带批处理上限, 可以硬性防止后台任务无限堆积。
    'ai-sweep-pending': {
        'task': 'rssant_ai.tasks.sweep_pending_stories',
        'schedule': 120.0,
        # 超过 110 秒还没被消费就直接丢弃, 避免队列积压后集中爆发
        'options': {'expires': 110},
    },
    # 每日 AI 资讯简报
    'ai-daily-digest': {
        'task': 'rssant_ai.tasks.generate_daily_digest',
        'schedule': crontab(hour=_digest_hour(), minute=_digest_minute()),
    },
    # 每周清理一次过期的 AI 数据, 控制表体积
    'ai-cleanup': {
        'task': 'rssant_ai.tasks.cleanup_old_ai_data',
        'schedule': crontab(hour=4, minute=30, day_of_week=0),
    },
}

# beat 写调度文件的目录(容器里挂到持久卷, 重启后不错过周期)
app.conf.beat_max_loop_interval = 60
