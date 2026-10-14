"""根据环境变量创建初始用户。

docker compose 首次启动后, 直接用 .env 里配置的用户名密码登录 /ai/ 即可,
不必再通过邮件注册(自托管环境通常没有配 SMTP)。

环境变量:
    RSSANT_SUPERUSER_USERNAME  默认 admin
    RSSANT_SUPERUSER_PASSWORD  留空则跳过
    RSSANT_SUPERUSER_EMAIL
"""

import logging
import os

from django.contrib.auth import get_user_model

LOG = logging.getLogger(__name__)


def run():
    username = os.getenv('RSSANT_SUPERUSER_USERNAME') or os.getenv('RSSANT_ADMIN_USERNAME')
    password = os.getenv('RSSANT_SUPERUSER_PASSWORD') or os.getenv('RSSANT_ADMIN_PASSWORD')
    email = os.getenv('RSSANT_SUPERUSER_EMAIL') or 'admin@example.com'
    if not username or not password:
        LOG.info('RSSANT_SUPERUSER_USERNAME/PASSWORD not set, skip create user')
        return
    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is not None:
        LOG.info('user %r already exists, skip', username)
        return
    User.objects.create_superuser(username=username, email=email, password=password)
    LOG.info('created superuser %r, login at /ai/', username)
