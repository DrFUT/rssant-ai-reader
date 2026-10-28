"""rssant_ai 数据模型。

刻意采用「独立的 AI 结果表」而不是往文章表上加字段，原因:

    1. rssant 的文章查询大量使用 django-seal + 字段裁剪(defer)，
       往主表塞大字段会拖慢所有阅读接口的查询
    2. AI 结果有自己的生命周期(排队 -> 处理 -> 失败重试)，
       独立表可以单独加索引并限流，不影响订阅同步
    3. 对上游 rssant 的改动量最小 —— 只增加一个外键关联，不改主表结构

关于关联目标: rssant 每篇文章真正落到 `rssant_api_storyinfo` 表
(StoryInfo, 主键 id = StoryId.encode(feed_id, offset))，正文另存
story_volume_* 卷表。`rssant_api_story`(Story) 是历史遗留表，实际为空，
不能拿它做关联。
"""

from django.conf import settings
from django.contrib.postgres.fields import JSONField
from django.db import models
from django.utils import timezone

from rssant_api.models.story_info import StoryInfo

from ai.prompts import DEFAULT_TOPIC

optional = dict(blank=True, null=True)


class AIStatus:
    """AI 处理状态机。

    pending    -> 排队中，等待 worker 处理
    processing -> 正在调用 LLM
    done       -> 处理成功
    failed     -> 调用失败(仍可重试)
    skipped    -> 主动跳过(未配置 AI / 正文为空 / 达到重试上限)
    """

    PENDING = 'pending'
    PROCESSING = 'processing'
    DONE = 'done'
    FAILED = 'failed'
    SKIPPED = 'skipped'

    CHOICES = [
        (PENDING, '排队中'),
        (PROCESSING, '处理中'),
        (DONE, '已完成'),
        (FAILED, '失败'),
        (SKIPPED, '已跳过'),
    ]

    RETRYABLE = (PENDING, FAILED)


class StoryAI(models.Model):
    """一篇文章的 AI 处理结果。"""

    class Meta:
        verbose_name = 'AI摘要'
        verbose_name_plural = 'AI摘要'
        indexes = [
            models.Index(fields=['status', 'ai_score'], name='rssant_ai_status_score_idx'),
            models.Index(fields=['status', 'dt_created'], name='rssant_ai_status_created_idx'),
            models.Index(fields=['topic', 'ai_score'], name='rssant_ai_topic_score_idx'),
        ]

    class Admin:
        display_fields = ['story_id', 'topic', 'ai_score', 'status']

    story = models.OneToOneField(
        StoryInfo, on_delete=models.CASCADE, related_name='ai', help_text='关联文章'
    )
    # 摘要
    ai_summary = models.TextField(blank=True, help_text='一句话总结')
    ai_points = JSONField(default=list, blank=True, help_text='关键观点列表')
    ai_keywords = JSONField(default=list, blank=True, help_text='关键词/标签')
    # 分类
    topic = models.CharField(
        max_length=32, default=DEFAULT_TOPIC, db_index=True, help_text='一级主题'
    )
    # 评分
    ai_score = models.IntegerField(default=0, db_index=True, help_text='价值评分 0-10')
    # 处理状态
    status = models.CharField(
        max_length=16,
        choices=AIStatus.CHOICES,
        default=AIStatus.PENDING,
        db_index=True,
        help_text='AI处理状态',
    )
    retry_count = models.IntegerField(default=0, help_text='已重试次数')
    error = models.TextField(blank=True, help_text='最近一次失败原因')
    model = models.CharField(max_length=64, blank=True, help_text='使用的模型')
    dt_processed = models.DateTimeField(**optional, help_text='处理完成时间')
    dt_created = models.DateTimeField(default=timezone.now, help_text='创建时间')
    dt_updated = models.DateTimeField(auto_now=True, help_text='更新时间')

    def __str__(self):
        return f'StoryAI#{self.id} story={self.story_id} topic={self.topic} score={self.ai_score}'

    def to_dict(self) -> dict:
        return {
            'ai_summary': self.ai_summary,
            'ai_points': self.ai_points or [],
            'ai_keywords': self.ai_keywords or [],
            'topic': self.topic,
            'ai_score': self.ai_score,
            'status': self.status,
            'model': self.model,
            'dt_processed': self.dt_processed,
        }


class DailyDigest(models.Model):
    """每日 AI 资讯简报。"""

    class Meta:
        verbose_name = '每日简报'
        verbose_name_plural = '每日简报'
        ordering = ['-date']

    class Admin:
        display_fields = ['date', 'story_count', 'status']

    date = models.DateField(unique=True, db_index=True, help_text='简报日期')
    overview = models.TextField(blank=True, help_text='今日概览')
    items = JSONField(default=list, blank=True, help_text='热点条目')
    markdown = models.TextField(blank=True, help_text='Markdown 渲染结果')
    story_count = models.IntegerField(default=0, help_text='候选文章数')
    model = models.CharField(max_length=64, blank=True, help_text='使用的模型')
    status = models.CharField(
        max_length=16, choices=AIStatus.CHOICES, default=AIStatus.DONE, help_text='状态'
    )
    error = models.TextField(blank=True, help_text='失败原因')
    dt_created = models.DateTimeField(default=timezone.now, help_text='创建时间')
    dt_updated = models.DateTimeField(auto_now=True, help_text='更新时间')

    def __str__(self):
        return f'DailyDigest#{self.id} date={self.date} items={len(self.items or [])}'

    def to_dict(self, include_markdown: bool = False) -> dict:
        data = {
            'date': self.date.isoformat() if self.date else None,
            'overview': self.overview,
            'items': self.items or [],
            'story_count': self.story_count,
            'model': self.model,
            'status': self.status,
            'dt_updated': self.dt_updated,
        }
        if include_markdown:
            data['markdown'] = self.markdown
        return data


class UserInterest(models.Model):
    """用户兴趣配置(轻量版)。

    只保存关键词与主题偏好，不做任何机器学习/向量检索，
    匹配逻辑见 ai/interest.py —— 可解释、零额外资源消耗。
    """

    class Meta:
        verbose_name = '兴趣配置'
        verbose_name_plural = '兴趣配置'

    class Admin:
        display_fields = ['user_id', 'keywords', 'enable']

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_interest',
        help_text='用户',
    )
    keywords = models.TextField(blank=True, help_text='关注关键词，逗号分隔')
    topics = JSONField(default=list, blank=True, help_text='关注主题列表')
    threshold = models.FloatField(default=1.0, help_text='命中阈值')
    enable = models.BooleanField(default=True, help_text='是否启用兴趣过滤')
    dt_created = models.DateTimeField(default=timezone.now, help_text='创建时间')
    dt_updated = models.DateTimeField(auto_now=True, help_text='更新时间')

    def __str__(self):
        return f'UserInterest#{self.id} user={self.user_id} keywords={self.keywords!r}'

    def to_dict(self) -> dict:
        return {
            'keywords': self.keywords,
            'topics': self.topics or [],
            'threshold': self.threshold,
            'enable': self.enable,
        }
