from django.urls import include, path

from .views import AIView

# 最终 URL: /api/v1/ai/*
urlpatterns = [
    path('', include(AIView.urls)),
]
