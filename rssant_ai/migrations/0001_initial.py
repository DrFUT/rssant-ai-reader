# rssant_ai 初始迁移
#
# 说明: 本迁移为 AI 增强功能新增三张表, 不修改 rssant 原有的任何表结构。
# 依赖 rssant_api 的最新迁移, 保证 Story / User 表已经存在。

import django.contrib.postgres.fields.jsonb
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


AI_STATUS_CHOICES = [
    ('pending', '排队中'),
    ('processing', '处理中'),
    ('done', '已完成'),
    ('failed', '失败'),
    ('skipped', '已跳过'),
]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('rssant_api', '0035_auto_20240828_0643'),
    ]

    operations = [
        migrations.CreateModel(
            name='StoryAI',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ai_summary', models.TextField(blank=True, help_text='一句话总结')),
                ('ai_points', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=list, help_text='关键观点列表')),
                ('ai_keywords', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=list, help_text='关键词/标签')),
                ('topic', models.CharField(db_index=True, default='其他', help_text='一级主题', max_length=32)),
                ('ai_score', models.IntegerField(db_index=True, default=0, help_text='价值评分 0-10')),
                ('status', models.CharField(choices=AI_STATUS_CHOICES, db_index=True, default='pending', help_text='AI处理状态', max_length=16)),
                ('retry_count', models.IntegerField(default=0, help_text='已重试次数')),
                ('error', models.TextField(blank=True, help_text='最近一次失败原因')),
                ('model', models.CharField(blank=True, help_text='使用的模型', max_length=64)),
                ('dt_processed', models.DateTimeField(blank=True, help_text='处理完成时间', null=True)),
                ('dt_created', models.DateTimeField(default=django.utils.timezone.now, help_text='创建时间')),
                ('dt_updated', models.DateTimeField(auto_now=True, help_text='更新时间')),
                ('story', models.OneToOneField(help_text='关联文章', on_delete=django.db.models.deletion.CASCADE, related_name='ai', to='rssant_api.Story')),
            ],
            options={
                'verbose_name': 'AI摘要',
                'verbose_name_plural': 'AI摘要',
            },
        ),
        migrations.CreateModel(
            name='DailyDigest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(help_text='简报日期', unique=True)),
                ('overview', models.TextField(blank=True, help_text='今日概览')),
                ('items', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=list, help_text='热点条目')),
                ('markdown', models.TextField(blank=True, help_text='Markdown 渲染结果')),
                ('story_count', models.IntegerField(default=0, help_text='候选文章数')),
                ('model', models.CharField(blank=True, help_text='使用的模型', max_length=64)),
                ('status', models.CharField(choices=AI_STATUS_CHOICES, default='done', help_text='状态', max_length=16)),
                ('error', models.TextField(blank=True, help_text='失败原因')),
                ('dt_created', models.DateTimeField(default=django.utils.timezone.now, help_text='创建时间')),
                ('dt_updated', models.DateTimeField(auto_now=True, help_text='更新时间')),
            ],
            options={
                'verbose_name': '每日简报',
                'verbose_name_plural': '每日简报',
                'ordering': ['-date'],
            },
        ),
        migrations.CreateModel(
            name='UserInterest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('keywords', models.TextField(blank=True, help_text='关注关键词，逗号分隔')),
                ('topics', django.contrib.postgres.fields.jsonb.JSONField(blank=True, default=list, help_text='关注主题列表')),
                ('threshold', models.FloatField(default=1.0, help_text='命中阈值')),
                ('enable', models.BooleanField(default=True, help_text='是否启用兴趣过滤')),
                ('dt_created', models.DateTimeField(default=django.utils.timezone.now, help_text='创建时间')),
                ('dt_updated', models.DateTimeField(auto_now=True, help_text='更新时间')),
                ('user', models.OneToOneField(help_text='用户', on_delete=django.db.models.deletion.CASCADE, related_name='ai_interest', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': '兴趣配置',
                'verbose_name_plural': '兴趣配置',
            },
        ),
        migrations.AddIndex(
            model_name='storyai',
            index=models.Index(fields=['status', 'ai_score'], name='rssant_ai_status_score_idx'),
        ),
        migrations.AddIndex(
            model_name='storyai',
            index=models.Index(fields=['status', 'dt_created'], name='rssant_ai_status_created_idx'),
        ),
        migrations.AddIndex(
            model_name='storyai',
            index=models.Index(fields=['topic', 'ai_score'], name='rssant_ai_topic_score_idx'),
        ),
    ]
