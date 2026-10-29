"""rssant_ai 测试公共夹具。

不直接复用上游 rssant_api 的测试基类，原因:
    * 这里只需要"最小可用的文章"，走 STORY_SERVICE 会连带写 volume 表，
      测试要额外准备存储后端，成本远大于收益
    * AI 相关断言只关心 StoryInfo 的主键映射(feed_id -> 主键区间)，
      所以直接构造 StoryInfo 行即可
"""

from django.contrib.auth import get_user_model
from django.utils import timezone

from rssant_api.models import Feed, FeedStatus, UserFeed
from rssant_api.models.story_info import StoryInfo
from rssant_api.models.story_storage.common.story_key import StoryId

from rssant_ai.models import AIStatus, StoryAI

User = get_user_model()


def make_user(username='tester'):
    return User.objects.create_user(username=username, password='test-password-123')


def make_feed(title='test feed', url='https://example.com/feed.xml'):
    feed = Feed(
        title=title,
        url=url,
        status=FeedStatus.READY,
        dt_updated=timezone.now(),
    )
    feed.save()
    return feed


def subscribe(user, feed):
    return UserFeed.objects.create(user=user, feed=feed)


def make_story(feed, offset, title=None, summary='测试摘要'):
    """直接落一条 StoryInfo。主键必须用 StoryId.encode 换算。"""
    offset = int(offset)
    return StoryInfo.objects.create(
        id=StoryId.encode(feed.id, offset),
        unique_id='feed%s/%s' % (feed.id, offset),
        title=title or ('测试文章 %s-%s' % (feed.id, offset)),
        link='https://example.com/%s/%s.html' % (feed.id, offset),
        dt_published=timezone.now(),
        summary=summary,
    )


def make_story_ai(story, status=AIStatus.DONE, topic='AI', score=7,
                  model='fallback', summary='摘要'):
    return StoryAI.objects.create(
        story=story,
        ai_summary=summary,
        ai_points=[],
        ai_keywords=['测试'],
        topic=topic,
        ai_score=score,
        status=status,
        model=model,
    )
