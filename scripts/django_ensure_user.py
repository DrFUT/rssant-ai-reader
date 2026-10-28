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
        # 上游 django_run_db_init 会先建一个 admin/admin 的超级用户,
        # 所以"用户已存在"几乎必然发生。若此时直接跳过, .env 里配置的
        # 密码永远不会生效, 登录必然失败。
        # 这里只在口令仍是上游默认值('admin')时才同步成 .env 的值 ——
        # 用户自己改过密码的情形不会被覆盖。
        if user.check_password(password):
            LOG.info('user %r already exists, password matches env, skip', username)
            return
        if user.check_password('admin') and password != 'admin':
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=['password', 'is_staff', 'is_superuser'])
            LOG.info('user %r still used upstream default password, synced with .env', username)
            return
        LOG.warning(
            'user %r exists with a different password, .env password NOT applied; '
            'use the existing password or change it in admin',
            username,
        )
        return
    User.objects.create_superuser(username=username, email=email, password=password)
    LOG.info('created superuser %r, login at /ai/', username)
