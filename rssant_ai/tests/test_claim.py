"""回归测试: 同一篇 AI 待办在多次/并发派发时最多只有一个执行者进入模型调用。

关键点: 保护必须来自数据库的原子 UPDATE，而不是"先读状态再判断"。
所以这里既测顺序重复调用，也测真正的并发争抢（多线程同时抢占同一行）。
模型调用用 stub 计数，不产生任何真实请求。
"""

import threading
from datetime import timedelta
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from ai.summarizer import StorySummary

from rssant_ai.models import AIStatus, StoryAI
from rssant_ai import service

from .factories import make_feed, make_story, make_story_ai, make_user, subscribe


class _FakeConfig:
    """只实现 service 用到的字段，避免依赖真实 .env。"""
    is_enabled = True
    max_task_retries = 2
    batch_size = 20
    sweep_pending_grace = 120
    model = 'stub-model'


class _Counter:
    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def hit(self):
        with self._lock:
            self.calls += 1


def _stub_summary(**kwargs):
    return StorySummary(
        summary='stub summary',
        points=['p1'],
        keywords=['k1'],
        topic='AI',
        score=8,
        model='stub-model',
    )


class ClaimTestCase(TransactionTestCase):

    def setUp(self):
        self.user = make_user('claimer')
        self.feed = make_feed()
        self.story = make_story(self.feed, 0)

    # ------------------------------------------------------------------
    # 原子抢占本身
    # ------------------------------------------------------------------
    def test_claim_only_once_for_pending(self):
        row = make_story_ai(self.story, status=AIStatus.PENDING)
        assert service.claim_story_ai(row.id) is True
        assert service.claim_story_ai(row.id) is False
        row.refresh_from_db()
        assert row.status == AIStatus.PROCESSING

    def test_claim_failed_row(self):
        row = make_story_ai(self.story, status=AIStatus.FAILED)
        assert service.claim_story_ai(row.id) is True

    def test_claim_done_row_is_rejected_unless_force(self):
        row = make_story_ai(self.story, status=AIStatus.DONE)
        assert service.claim_story_ai(row.id) is False
        assert service.claim_story_ai(row.id, force=True) is True

    def test_claim_missing_row(self):
        assert service.claim_story_ai(99999999) is False

    # ------------------------------------------------------------------
    # 顺序重复处理
    # ------------------------------------------------------------------
    def test_second_process_call_does_not_hit_model(self):
        row = make_story_ai(self.story, status=AIStatus.PENDING)
        counter = _Counter()
        with mock.patch.object(service, 'get_config', return_value=_FakeConfig()), \
                mock.patch.object(service, '_load_content', return_value='正文' * 100), \
                mock.patch.object(service, 'summarize_story',
                                  side_effect=lambda **kw: (counter.hit(), _stub_summary())[1]):
            first = service.process_story_ai(row.id)
            second = service.process_story_ai(row.id)

        assert first is True
        assert second is False
        assert counter.calls == 1, '同一篇被调用了 %s 次模型' % counter.calls
        row.refresh_from_db()
        assert row.status == AIStatus.DONE
        assert row.model == 'stub-model'

    # ------------------------------------------------------------------
    # 真并发：两个线程同时抢同一行
    # ------------------------------------------------------------------
    def test_concurrent_dispatch_calls_model_once(self):
        row = make_story_ai(self.story, status=AIStatus.PENDING)
        counter = _Counter()
        barrier = threading.Barrier(2)
        results = []

        def worker():
            try:
                barrier.wait(timeout=10)
                ok = service.process_story_ai(row.id)
                results.append(bool(ok))
            finally:
                # 线程里用的是独立连接，必须显式关闭，否则测试收尾 truncate 会卡住
                connections.close_all()

        with mock.patch.object(service, 'get_config', return_value=_FakeConfig()), \
                mock.patch.object(service, '_load_content', return_value='正文' * 100), \
                mock.patch.object(service, 'summarize_story',
                                  side_effect=lambda **kw: (counter.hit(), _stub_summary())[1]):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                assert not t.is_alive(), '并发线程未在 30s 内结束'

        assert counter.calls == 1, '并发下模型被调用了 %s 次' % counter.calls
        assert results.count(True) == 1, results


class SweepTestCase(TransactionTestCase):

    def setUp(self):
        self.user = make_user('sweeper')
        self.feed = make_feed()
        self.story = make_story(self.feed, 0)

    def _sweep(self):
        with mock.patch.object(service, 'get_config', return_value=_FakeConfig()), \
                mock.patch.object(service, 'process_story_ai', return_value=True) as m:
            result = service.sweep_pending(limit=10)
        return result, m

    def test_fresh_pending_is_not_swept(self):
        """刚建/刚派发的 pending 不立刻巡查，避免和队列抢同一篇。"""
        make_story_ai(self.story, status=AIStatus.PENDING)
        result, mock_process = self._sweep()
        assert result['total'] == 0
        assert mock_process.call_count == 0

    def test_stale_pending_is_swept(self):
        row = make_story_ai(self.story, status=AIStatus.PENDING)
        StoryAI.objects.filter(id=row.id).update(
            dt_updated=timezone.now() - timedelta(seconds=600)
        )
        result, mock_process = self._sweep()
        assert result['total'] == 1
        assert mock_process.call_count == 1

    def test_failed_is_swept_immediately(self):
        make_story_ai(self.story, status=AIStatus.FAILED)
        result, mock_process = self._sweep()
        assert result['total'] == 1

    def test_processing_row_is_not_swept(self):
        make_story_ai(self.story, status=AIStatus.PROCESSING)
        result, _ = self._sweep()
        assert result['total'] == 0

    def test_stale_processing_is_reclaimed_then_swept(self):
        """崩溃后卡在 processing 的行必须能被回收，否则永远不再处理。"""
        row = make_story_ai(self.story, status=AIStatus.PROCESSING)
        StoryAI.objects.filter(id=row.id).update(
            dt_updated=timezone.now() - timedelta(minutes=30)
        )
        # 回收会把它改回 pending 并把 dt_updated 刷新为现在，
        # 因此这一轮巡查不会立刻处理它（要再等一个宽限期）
        result, _ = self._sweep()
        row.refresh_from_db()
        assert row.status == AIStatus.PENDING
        assert result['total'] == 0


class RefreshTestCase(TransactionTestCase):
    """手动刷新不能绕过并发保护。"""

    def setUp(self):
        self.user = make_user('refresher')
        self.feed = make_feed()
        subscribe(self.user, self.feed)
        self.story = make_story(self.feed, 0)

    def test_refresh_rejected_while_processing(self):
        row = make_story_ai(self.story, status=AIStatus.PROCESSING)
        self.client.force_login(self.user)
        response = self.client.post('/api/v1/ai/story/%s/refresh' % self.story.id)
        assert response.status_code == 409, response.content[:300]
        row.refresh_from_db()
        assert row.status == AIStatus.PROCESSING

    def test_refresh_resets_done_row(self):
        row = make_story_ai(self.story, status=AIStatus.DONE)
        self.client.force_login(self.user)
        with mock.patch('rssant_ai.views._dispatch_summarize') as m:
            response = self.client.post('/api/v1/ai/story/%s/refresh' % self.story.id)
        assert response.status_code == 200, response.content[:300]
        row.refresh_from_db()
        assert row.status == AIStatus.PENDING
        assert m.call_count == 1
