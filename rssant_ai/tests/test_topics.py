"""回归测试: /api/v1/ai/topics 不再 500，且只统计当前用户订阅的文章。

背景: 旧实现用 `story__feed_id__in=feed_ids` 过滤，而 StoryInfo.feed_id
只是 Python property(由 StoryId.decode 算出)，数据库里没有这一列，
线上必然抛 FieldError -> 500。这个测试锁住修复。
"""

import json

import pytest
from django.test import TransactionTestCase

from rssant_ai.models import AIStatus

from .factories import (
    make_feed,
    make_story,
    make_story_ai,
    make_user,
    subscribe,
)

URL = '/api/v1/ai/topics'


class AITopicsTestCase(TransactionTestCase):

    def _topics(self, user):
        self.client.force_login(user)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.content[:300])
        data = json.loads(response.content)
        return {x['topic']: x['count'] for x in data['topics']}

    def test_single_feed(self):
        user = make_user('u1')
        feed = make_feed('订阅源一', 'https://example.com/a.xml')
        subscribe(user, feed)
        make_story_ai(make_story(feed, 0, 'AI 文章'), topic='AI')
        make_story_ai(make_story(feed, 1, '技术文章'), topic='技术')

        counts = self._topics(user)
        assert counts['AI'] == 1
        assert counts['技术'] == 1
        assert counts['商业'] == 0
        # 枚举固定返回 7 个主题，前端筛选器依赖这个契约
        assert len(counts) == 7

    def test_multiple_feeds_are_summed(self):
        user = make_user('u1')
        feed_a = make_feed('订阅源一', 'https://example.com/a.xml')
        feed_b = make_feed('订阅源二', 'https://example.com/b.xml')
        subscribe(user, feed_a)
        subscribe(user, feed_b)
        make_story_ai(make_story(feed_a, 0), topic='AI')
        make_story_ai(make_story(feed_b, 0), topic='AI')
        make_story_ai(make_story(feed_b, 1), topic='编程')

        counts = self._topics(user)
        assert counts['AI'] == 2
        assert counts['编程'] == 1

    def test_no_feed_returns_zero_instead_of_global_counts(self):
        """没有订阅的用户必须全 0，不能退化成全库统计（数据隔离）。"""
        owner = make_user('owner')
        feed = make_feed()
        subscribe(owner, feed)
        make_story_ai(make_story(feed, 0), topic='AI')

        nobody = make_user('nobody')
        counts = self._topics(nobody)
        assert sum(counts.values()) == 0, counts

    def test_other_users_feed_is_excluded(self):
        alice = make_user('alice')
        bob = make_user('bob')
        feed_a = make_feed('A', 'https://example.com/a.xml')
        feed_b = make_feed('B', 'https://example.com/b.xml')
        subscribe(alice, feed_a)
        subscribe(bob, feed_b)
        make_story_ai(make_story(feed_a, 0), topic='AI')
        make_story_ai(make_story(feed_b, 0), topic='商业')

        counts = self._topics(alice)
        assert counts['AI'] == 1
        assert counts['商业'] == 0

    def test_only_done_rows_are_counted(self):
        user = make_user('u1')
        feed = make_feed()
        subscribe(user, feed)
        make_story_ai(make_story(feed, 0), status=AIStatus.PENDING, topic='AI')
        make_story_ai(make_story(feed, 1), status=AIStatus.FAILED, topic='AI')
        make_story_ai(make_story(feed, 2), status=AIStatus.DONE, topic='AI')

        counts = self._topics(user)
        assert counts['AI'] == 1


@pytest.mark.dbtest
class AITopicsSmokeTestCase(TransactionTestCase):
    """空库下也不应 500。"""

    def test_empty_database(self):
        user = make_user('lonely')
        self.client.force_login(user)
        response = self.client.get(URL)
        assert response.status_code == 200
