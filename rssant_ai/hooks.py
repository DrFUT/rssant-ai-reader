"""RSS 同步链路的挂载点。

rssant 抓取并落库文章后调用 on_stories_saved(), 这里只做两件很轻的事:
    1. 建 AI 待办记录(一次批量 INSERT)
    2. 给 Celery 派发任务(有上限)

真正的 LLM 调用永远不发生在订阅同步的请求链路里，
并且任何异常都被吞掉 —— AI 功能挂了绝不能影响 RSS 订阅本身。
"""

import logging

LOG = logging.getLogger(__name__)


def on_stories_saved(feed_id: int, storys) -> dict:
    """由 rssant_api.models.story_service.bulk_save_by_feed 调用。"""
    try:
        from .service import on_new_stories

        return on_new_stories(feed_id, storys)
    except Exception as ex:  # noqa: BLE001
        # AI 是增强功能, 不能因为它的异常让订阅同步失败
        LOG.warning('ai hook on_stories_saved failed feed=%s: %s', feed_id, ex)
        return {'created': 0, 'dispatched': 0}
