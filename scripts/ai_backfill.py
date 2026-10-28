"""给已经入库的历史文章补建 AI 待办。

背景: 文章入库时由 `rssant_ai.hooks.on_stories_saved` 创建待办，
      部署完成后才启用 AI、或修复了钩子之后，历史文章不会再次经过钩子。
      这个脚本一次性补齐，之后由 Celery 的巡查任务（每 120 秒）自动处理，
      不需要手动派发任务。

用法:
    docker compose exec web python manage.py runscript ai_backfill
    docker compose exec web python manage.py runscript ai_backfill --script-args 200
"""

import logging

from rssant_api.models.story_info import StoryInfo

from rssant_ai.service import ensure_story_ai

LOG = logging.getLogger(__name__)


def run(limit=100):
    limit = int(limit)
    story_ids = list(
        StoryInfo.objects.order_by('-dt_created').values_list('id', flat=True)[:limit]
    )
    new_ids = ensure_story_ai(story_ids)
    print('ai_backfill: stories=%s new_ai_tasks=%s (剩余由巡查任务处理)'
          % (len(story_ids), len(new_ids)))
    LOG.info('ai backfill stories=%s new=%s', len(story_ids), len(new_ids))
