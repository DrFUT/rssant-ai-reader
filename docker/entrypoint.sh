#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 容器入口
#
#   entrypoint.sh web      启动 Django/gunicorn(API + 阅读器页面), 并负责建表
#   entrypoint.sh worker   启动后台任务容器(Celery AI worker + RSS 同步管线)
#   entrypoint.sh beat     启动 Celery Beat 定时调度
#   entrypoint.sh migrate  只跑数据库迁移
#
# 说明: 迁移只由 web 这一个容器执行。worker / beat 通过 compose 的
#       depends_on: service_healthy 等待 web 就绪, 避免多容器并发 migrate。
# ---------------------------------------------------------------------------
set -e

ROLE="${1:-web}"

wait_postgres() {
    echo "[entrypoint] waiting for postgres ${RSSANT_PG_HOST:-database}:${RSSANT_PG_PORT:-5432} ..."
    python - <<'PY'
import os
import sys
import time

import psycopg2

host = os.getenv('RSSANT_PG_HOST', 'database')
port = int(os.getenv('RSSANT_PG_PORT', '5432'))
user = os.getenv('RSSANT_PG_USER', 'rssant')
password = os.getenv('RSSANT_PG_PASSWORD', 'rssant')

deadline = time.time() + 180
while time.time() < deadline:
    try:
        conn = psycopg2.connect(
            host=host, port=port, user=user, password=password,
            dbname='postgres', connect_timeout=3,
        )
        conn.close()
        print('[entrypoint] postgres is ready')
        sys.exit(0)
    except Exception as ex:  # noqa: BLE001
        print('[entrypoint] postgres not ready: %s' % ex)
        time.sleep(2)
print('[entrypoint] postgres wait timeout')
sys.exit(1)
PY
}

case "$ROLE" in
web)
    wait_postgres
    python manage.py runscript django_pre_migrate || true
    python manage.py migrate --noinput
    python manage.py runscript django_db_init || true
    python manage.py runscript django_ensure_user || true
    export RSSANT_ROLE=api
    export RSSANT_BIND_ADDRESS="${RSSANT_BIND_ADDRESS:-0.0.0.0:6789}"
    echo "[entrypoint] starting web on ${RSSANT_BIND_ADDRESS}"
    exec python runserver.py
    ;;
worker)
    wait_postgres
    echo "[entrypoint] starting background worker container"
    exec supervisord -c /app/docker/supervisord-worker.conf -n
    ;;
beat)
    wait_postgres
    export RSSANT_ROLE=api
    echo "[entrypoint] starting celery beat"
    exec celery -A rssant_ai beat -l info --schedule /app/data/celerybeat-schedule
    ;;
migrate)
    wait_postgres
    python manage.py runscript django_pre_migrate || true
    python manage.py migrate --noinput
    python manage.py runscript django_db_init || true
    ;;
*)
    exec "$@"
    ;;
esac
