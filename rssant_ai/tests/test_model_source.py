"""回归测试: 接口与前端都能区分"真实模型"和"规则降级"。

判据只有 model 字段:
    model='fallback'  -> 规则降级(未配置 Key 或调用失败)
    model='<模型名>'  -> 真实 LLM
    model=''          -> 历史数据，两者都不能假定
status=done 不能作为"AI 成功"的证据，因为降级结果也是 done。
"""

import json
from pathlib import Path

from django.conf import settings
from django.test import TransactionTestCase

from rssant_ai.models import AIStatus
from rssant_ai.views import _story_dict

from .factories import make_feed, make_story, make_story_ai, make_user, subscribe


class StoryDictTestCase(TransactionTestCase):

    def setUp(self):
        self.user = make_user('reader')
        self.feed = make_feed()
        self.story = make_story(self.feed, 0, '测试文章')

    def test_fallback_model_is_exposed(self):
        row = make_story_ai(self.story, model='fallback')
        data = _story_dict(row)
        assert data['model'] == 'fallback'
        # 降级同样是 done，所以不能靠 status 判断
        assert data['ai_status'] == AIStatus.DONE

    def test_real_model_is_exposed(self):
        row = make_story_ai(self.story, model='deepseek-chat')
        assert _story_dict(row)['model'] == 'deepseek-chat'

    def test_missing_model_falls_back_to_empty_string(self):
        """历史记录没有 model 值，接口必须给空串而不是 None。"""
        row = make_story_ai(self.story, model='')
        assert _story_dict(row)['model'] == ''

    def test_payload_contains_no_secrets(self):
        row = make_story_ai(self.story, model='fallback')
        data = _story_dict(row)
        dumped = json.dumps(data, default=str).lower()
        for bad in ('api_key', 'apikey', 'authorization', 'bearer', 'sk-', 'secret'):
            assert bad not in dumped, '接口不应出现 %r' % bad

    def test_list_api_returns_model(self):
        subscribe(self.user, self.feed)
        make_story_ai(self.story, model='fallback')
        self.client.force_login(self.user)
        response = self.client.get('/api/v1/ai/stories?limit=1')
        assert response.status_code == 200, response.content[:300]
        body = json.loads(response.content)
        assert body['total'] == 1
        assert body['stories'][0]['model'] == 'fallback'


class ReaderTemplateTestCase(TransactionTestCase):
    """前端契约: 模板里必须按 model 判断降级，不能再按 ai_status 判断。"""

    def setUp(self):
        path = Path(settings.BASE_DIR) / 'rssant_ai' / 'templates' / 'rssant_ai' / 'reader.html'
        self.html = path.read_text(encoding='utf-8')

    def test_template_marks_fallback_by_model(self):
        assert "model === 'fallback'" in self.html
        assert '规则降级' in self.html

    def test_template_no_longer_infers_from_status(self):
        # 旧写法: 用 ai_status 判断是否降级 —— 已证明是死代码
        assert "s.ai_status === 'done' ? ''" not in self.html
