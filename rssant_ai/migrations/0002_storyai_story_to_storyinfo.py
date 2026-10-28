"""把 AI 结果表的关联目标从 Story 改成 StoryInfo。

背景: rssant 每篇文章真正落库的是 `rssant_api_storyinfo`
(StoryInfo, 主键 = StoryId.encode(feed_id, offset))，正文另存
story_volume_* 卷表；`rssant_api_story`(Story) 是历史遗留表，实际为空。
原来关联 Story 会导致订阅同步后一篇文章都关联不上，AI 任务永远不会创建。
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rssant_ai', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='storyai',
            name='story',
            field=models.OneToOneField(
                help_text='关联文章',
                on_delete=django.db.models.deletion.CASCADE,
                related_name='ai',
                to='rssant_api.storyinfo',
            ),
        ),
    ]
