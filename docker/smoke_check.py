"""部署后静态冒烟: 不连数据库, 只验证 import / 任务注册 / 配置解析。

用法(在宿主机上):
    docker compose exec -T web python docker/smoke_check.py

它能在"服务起来了但功能不对"的时候快速定位:
    * Celery 任务有没有注册上
    * 低配参数(并发/预取/超时)有没有真的生效
    * AI 层能否正常 import, 无 Key 时是否正确降级
"""
import os
import sys

# 本文件在 docker/ 下, 项目根目录是它的上一级
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rssant.settings')
os.environ.setdefault('RSSANT_ROLE', 'api')

import django

django.setup()

print('--- 1. Celery app / beat 定时表 ---')
from rssant_ai.celery import app as celery_app

for name, entry in celery_app.conf.beat_schedule.items():
    print('   %-18s %-42s schedule=%s' % (name, entry['task'], entry['schedule']))

print('--- 2. 任务注册 ---')
import rssant_ai.tasks  # noqa

names = sorted(n for n in celery_app.tasks if n.startswith('rssant_ai'))
for n in names:
    print('   ', n)
assert len(names) >= 5, names

print('--- 3. Celery 低配参数 ---')
for k in (
    'broker_url',
    'result_backend',
    'worker_concurrency',
    'worker_prefetch_multiplier',
    'task_acks_late',
    'task_soft_time_limit',
    'task_time_limit',
    'worker_max_tasks_per_child',
):
    print('   %-28s %s' % (k, getattr(celery_app.conf, k, '<unset>')))

print('--- 4. 视图与钩子可导入 ---')
import rssant_ai.hooks as h
import rssant_ai.service as s
import rssant_ai.views as v

print('   AIView 路由数:', len(v.AIView.urls))
print('   钩子:', h.on_stories_saved.__name__)
print('   service:', s.sweep_pending.__name__, s.process_story_ai.__name__)

print('--- 5. AI 层(无 Key 时走降级) ---')
from ai.config import get_config

cfg = get_config()
print('   enabled=%s base_url=%s model=%s' % (cfg.is_enabled, cfg.base_url, cfg.model))

from ai.classifier import classify_by_rules
from ai.summarizer import fallback_summary

print('   规则分类(AI):', classify_by_rules('DeepSeek 发布新一代大模型', '模型推理能力提升'))
print('   规则分类(Rust):', classify_by_rules('Rust 1.80 发布', 'a new trait feature'))
print('   降级摘要:', fallback_summary('标题', '正文' * 20).summary[:40], '...')

print('--- 6. 兴趣匹配词边界 ---')
from ai.text import contains

print('   contains("a new trait feature", "ai") =', contains('a new trait feature', 'ai'))
print('   contains("DeepSeek AI 模型",  "ai") =', contains('DeepSeek AI 模型', 'ai'))

print('\n全部静态冒烟通过')
