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
# setuptools / wheel / pip 全部锁到老版本, 后面的源码构建依赖它们
RUN pip install --no-cache-dir -r requirements-pip.txt

COPY constraint.txt .
ENV PIP_CONSTRAINT=/app/constraint.txt

# 预装源码构建可能需要的工具(部分老包没有 cp38 轮子时需要本地编译)。
# 注意: PIP_CONSTRAINT 已生效(见上), 所以这里装到的是 cython 2.x。
RUN pip install --no-cache-dir "cython<3"

COPY requirements.txt .
# ---------------------------------------------------------------------------
# 关键 1: 老包的 setup.py 与新版 setuptools 不兼容。
#   django-allauth==0.44.0 的 setup.py 用了 setuptools.convert_path, 该 API
#   在 setuptools>=60 已被移除; 而 pip 的"构建隔离"会在临时环境里装最新
#   setuptools, 直接报 ImportError: cannot import name 'convert_path'。
#   所以这一类老包必须关掉隔离(--no-build-isolation), 用上面锁定的
#   setuptools 51.3.3。
#
#   但反过来, 完全关掉隔离会让按 PEP 517 打包的新包拿不到它们声明的
#   构建依赖。两者风险方向相反, 所以这里做成一条兜底链:
#     第一次: 关隔离(覆盖所有老包, 这是主要矛盾)
#     第二次: 万一失败, 说明某个包需要隔离环境里的构建后端, 退回默认模式
#             —— 此时第一次已装好的包会被 pip 视为"已满足"而跳过,
#             不会整轮重来, 只补装剩下的那一个。
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir --no-build-isolation -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

# 关键 2: redis-py 单独装并跳过依赖声明。
#   redis>=4.5 声明 async-timeout>=4.0.2, 而 rssant 锁定的 aiohttp 3.7.4
#   要求 async-timeout<4.0, 两者无法同时满足(ResolutionImpossible)。
#   Celery/kombu 只用 redis 的同步 API, async-timeout 仅服务于 redis.asyncio,
#   环境里已有的 async-timeout 3.0.1 与其用法兼容, 因此跳过依赖声明是安全的。
RUN pip install --no-cache-dir --no-deps redis==4.6.0


FROM python:3.8.12-slim-bullseye

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=rssant.settings \
    TZ=Asia/Shanghai

# 国内云机房访问 deb.debian.org 很慢, main/updates 切阿里云镜像。
# bullseye 的 LTS 已于 2026-08 结束, security 仓库正在被各镜像下架
# (索引还在、包文件已搬走, 全网 404), 因此 security 必须改用永久冻结的
# archive.debian.org, 并关掉 Valid-Until 校验(归档库的 Release 已过期)。
# 另外云主机开启 IPv6 后 apt 优先走 v6 会黑洞超时, 强制 IPv4。
RUN sed -i 's|deb.debian.org/debian-security|archive.debian.org/debian-security|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; s|deb.debian.org/debian |mirrors.aliyun.com/debian |g' /etc/apt/sources.list \
    && printf 'Acquire::ForceIPv4 "true";\nAcquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99force-ipv4

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

# 首次启动时 web 要先跑完 migrate(几十个迁移), 这段时间内 curl 必然失败。
# 所以 start-period 给到 120s, 且重试次数放大到 20 次(约 10 分钟容错),
# 否则 compose 会因为 "web unhealthy" 直接拒绝启动 worker / beat。
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=20 \
    CMD curl -fsS http://127.0.0.1:6789/api/v1/health || exit 1

CMD ["/app/docker/entrypoint.sh", "web"]
