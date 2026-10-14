# ===========================================================================
# AI 智能信息流阅读器 (基于 rssant / 蚁阅 二次开发)
#
# 与上游 box/Dockerfile 的差异(为了能在 2 核 2GB 上跑起来):
#   1. 去掉 node 构建阶段 —— 上游前端 box/web 并未开源, 本项目的阅读器页面
#      是零构建步骤的 Django 模板, 不需要 npm
#   2. 去掉容器内 nginx / postgresql —— 交给 compose 的独立容器, 减少常驻进程
#   3. 依赖安装方式保持与上游一致, 保证 Python 3.8 下的老依赖能编译通过
# ===========================================================================

FROM python:3.8.12-bullseye AS build

WORKDIR /app

ARG PYPI_MIRROR="https://mirrors.aliyun.com/pypi/simple/"
ENV PIP_INDEX_URL=$PYPI_MIRROR \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv .venv
ENV PATH="/app/.venv/bin:$PATH"

COPY requirements-pip.txt .
RUN pip install --no-cache-dir -r requirements-pip.txt

COPY constraint.txt .
ENV PIP_CONSTRAINT=/app/constraint.txt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.8.12-slim-bullseye

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=rssant.settings \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libpq5 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/.venv /app/.venv

COPY . .

# Windows 上 clone 过来会丢掉可执行位, 这里统一补一次
RUN mkdir -p /app/data /app/logs && chmod +x \
    /app/docker/entrypoint.sh \
    /app/manage.py \
    /app/runserver.py \
    /app/run-scheduler.py \
    /app/run-asyncapi.py

# 静态文件收集: 本项目的阅读器页面是内联资源, 不依赖 manifest,
# 即使这里失败也不应该阻断镜像构建
RUN python manage.py collectstatic --noinput || echo "[warn] collectstatic skipped"

EXPOSE 6789

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS http://127.0.0.1:6789/api/v1/health || exit 1

CMD ["/app/docker/entrypoint.sh", "web"]
