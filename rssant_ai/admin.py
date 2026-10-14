from django.contrib import admin

from .models import DailyDigest, StoryAI, UserInterest


@admin.register(StoryAI)
class StoryAIAdmin(admin.ModelAdmin):
    list_display = ('id', 'story_id', 'topic', 'ai_score', 'status', 'retry_count', 'dt_processed')
    list_filter = ('status', 'topic')
    search_fields = ('story__title', 'ai_summary')
    readonly_fields = ('dt_created', 'dt_updated')
    ordering = ('-id',)


@admin.register(DailyDigest)
class DailyDigestAdmin(admin.ModelAdmin):
    list_display = ('id', 'date', 'story_count', 'status', 'model', 'dt_updated')
    list_filter = ('status',)
    ordering = ('-date',)


@admin.register(UserInterest)
class UserInterestAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'keywords', 'topics', 'enable')
    list_filter = ('enable',)
