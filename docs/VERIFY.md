# 部署验收手册

面向 **Ubuntu Server 2 核 2GB** 的逐项验收步骤。按顺序执行，每一步都给出了"怎样算通过"。

> 说明：本手册由开发机（Windows，无 Docker）静态验证后编写。
> 依赖安装、迁移一致性、Celery 注册、Django 检查等已在 Python 3.8 真实环境验证通过；
> 构建与端到端链路需要在你的 Ubuntu 上执行确认。

---

## 0. 前置

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
docker --version && docker compose version
```

确认 `.env` 已配置：

```bash
cp .env.example .env
# 必改: RSSANT_SECRET_KEY
# 想测 AI: AI_API_KEY / AI_BASE_URL / AI_MODEL
```

---

## 1. 构建镜像

```bash
docker compose build 2>&1 | tee build.log
```

**预期耗时**：8～20 分钟（Python 3.8 的老依赖较多）。

**构建期已修复的三个坑**（若你改了 requirements 可能复现）：

| 症状 | 原因 | 修复位置 |
|---|---|---|
| `ImportError: cannot import name 'convert_path' from 'setuptools'` | django-allauth 0.44 的老 setup.py 用了 setuptools≥60 已移除的 API，而 pip 构建隔离会装最新 setuptools | Dockerfile 先 `--no-build-isolation`（用锁定的 setuptools 51.3.3），失败再自动退回隔离模式重试 |
| `ResolutionImpossible ... python-dateutil` | celery 5.3.6 要求 dateutil≥2.8.2，rssant 锁的是 2.8.1 | `requirements.txt` 升到 2.8.2 |
| `ResolutionImpossible ... async-timeout` | redis≥4.5 要 async-timeout≥4.0.2，aiohttp 3.7.4 要 <4.0 | redis 用 `--no-deps` 单独安装 |

**通过标准**：`build.log` 末尾无 `ERROR`，`docker images | grep rssant-ai` 能看到镜像。

---

## 2. 启动

```bash
docker compose up -d
docker compose ps
```

**通过标准**：5 个服务全部 `Up`（`database` / `redis` / `web` 为 `Up (healthy)`）。

启动顺序是受控的：`database`/`redis` 健康 → `web` 建表 → `worker` → `beat`。首次启动建表约需 30～60 秒。

> 首次建表期间 `web` 暂时不响应健康检查，这是正常的：Dockerfile 里的
> `HEALTHCHECK` 已放宽到 `start-period=120s` + 20 次重试（约 10 分钟容错），
> 慢机器上也不会让 compose 误判 `web unhealthy` 而拒绝拉起 `worker` / `beat`。

若某个服务没起来：

```bash
docker compose logs --tail=80 web      # 建表 / gunicorn
docker compose logs --tail=80 worker   # Celery + RSS 调度器 + 抓取器
docker compose logs --tail=80 beat
```

---

## 3. 数据库迁移

迁移由 `web` 容器首次启动时自动执行（entrypoint 里），无需手动跑。
如需手动重跑：

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py showmigrations rssant_ai
```

**通过标准**：`showmigrations rssant_ai` 显示 `[X] 0001_initial`。

顺便确认 AI 三张表已建立：

```bash
docker compose exec database psql -U rssant -d rssant -c '\dt rssant_ai*'
```

应看到 `rssant_ai_storyai` / `rssant_ai_dailydigest` / `rssant_ai_userinterest`。

### 3.1 容器内静态冒烟（推荐先跑这一步）

```bash
docker compose exec -T web python docker/smoke_check.py
```

它不连数据库，只验证「代码层面是否健康」：

- Celery 三个定时任务已注册（`ai-sweep-pending` / `ai-daily-digest` / `ai-cleanup`）
- 五个任务已注册（`summarize_story_task` / `sweep_pending_stories` / `generate_daily_digest` / `cleanup_old_ai_data` / `ai_health_check`）
- 低配参数确实生效：`worker_concurrency=1`、`prefetch=1`、`acks_late=True`、软/硬超时 90/120s、`max-tasks-per-child=50`
- AI 层能 import；无 Key 时 `enabled=False` 并走规则降级
- 关键词词边界正确（`trait` 不再被误判成 `ai`）

**通过标准**：末尾输出「全部静态冒烟通过」。

---

## 4. 端到端链路验证

### 4.1 取登录 Token

```bash
TOKEN=$(curl -s -X POST http://localhost:6789/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123456"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
echo "TOKEN=$TOKEN"
```

> 上游 `django_db_init` 还会创建一个 `admin` / `admin` 用户（rssant 自带行为），
> 本项目的 `.env` 里 `RSSANT_SUPERUSER_*` 若改了用户名密码，用你改的那个。

### 4.2 确认 AI 状态

```bash
curl -s -H "Authorization: Token $TOKEN" http://localhost:6789/api/v1/ai/status | python3 -m json.tool
```

期望输出片段：

```json
{ "enabled": true, "model": "deepseek-chat", "queue": {"pending": 0, "done": 12, ...} }
```

`enabled=false` 表示没读到 `AI_API_KEY` —— 此时系统降级为规则分类，不会报错。

### 4.3 导入订阅源（RSS 文章导入）

```bash
curl -s -X POST http://localhost:6789/api/v1/feed.import \
  -H "Authorization: Token $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"https://www.ruanyifeng.com/blog/atom.xml\nhttps://feeds.appinn.com/appinns/"}' \
  | python3 -m json.tool
```

### 4.4 等待抓取

```bash
docker compose logs -f worker | grep -iE "sync_feed|find_feed|ai "
```

新订阅会立刻触发一次同步。`RSSANT_CHECK_FEED_MINUTES=1` 可加快验证（在 `.env` 里改）。

### 4.5 验证"数据库存储 + AI 任务触发 + 摘要生成 + 分类保存"

```bash
# ① 文章是否入库
docker compose exec database psql -U rssant -d rssant -c 'select count(*) from rssant_api_storyinfo;'

# ② AI 待办是否建立
docker compose exec database psql -U rssant -d rssant \
  -c 'select status, count(*) from rssant_ai_storyai group by status;'

# ③ AI 结果是否写回(最关键)
docker compose exec database psql -U rssant -d rssant -c \
  'select id, topic, ai_score, status, left(ai_summary, 40) as summary
   from rssant_ai_storyai where status = '"'"'done'"'"' order by id desc limit 5;'
```

**通过标准**：能看到 `topic` 有值、`ai_summary` 非空的行。

> 关于 `status` 的两种正常结果：
> - `done` —— 真实调用了 LLM（`.env` 里配了 `AI_API_KEY`）
> - `skipped` —— 没配 Key 时的**规则降级**结果，同样会写入 `topic` / `ai_summary` / `ai_score`，
>   `model` 列显示 `fallback`。这不是失败，只是没有 LLM 增强。
>
> 只有两者都为 0 才是真的没跑起来。一键脚本 `docker/verify.sh` 用的是
> `done + skipped` 的合计口径。

也可以走接口看：

```bash
curl -s -H "Authorization: Token $TOKEN" \
  'http://localhost:6789/api/v1/ai/stories?limit=3&sort=score' | python3 -m json.tool
```

### 4.6 验证页面展示

浏览器打开 <http://localhost:6789/ai/>，用 admin 登录。
应看到：信息流卡片带 AI 摘要、关键观点、主题标签、评分；顶部徽章显示"AI 已启用 · deepseek-chat"。

### 4.7 一键跑完上面所有检查

```bash
bash docker/verify.sh --import-feed --digest
```

不带参数时只做只读检查（不会导入订阅、不会消耗 API 额度）：

```bash
bash docker/verify.sh
```

它会自动读取 `.env` 里的账号与库名，依次检查：服务状态 → 迁移/表 → 登录 → AI 状态
→ 订阅导入 → 文章入库与 AI 摘要写回 → 信息流接口 → 每日简报 → 资源占用。
末尾输出「验收通过」或列出失败项。

---

## 5. 用真实 API 测一次每日简报

确保 `.env` 里填了真实 `AI_API_KEY`，然后：

```bash
docker compose up -d   # 让新 .env 生效(环境变量变了需要重建容器)
```

同步生成一次（便于立刻看到结果，不用等到定时）：

```bash
curl -s -X POST http://localhost:6789/api/v1/ai/digest/generate \
  -H "Authorization: Token $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"async":0}' | python3 -m json.tool
```

**通过标准**：返回 `{date, overview, items:[{story_id,title,digest,reason,score,link}], story_count, model}`，
`items` 非空，`reason` 有内容。

再看页面「每日简报」标签页，以及：

```bash
curl -s -H "Authorization: Token $TOKEN" http://localhost:6789/api/v1/ai/digest/latest
```

> 注意：简报只从**最近 24 小时入库且已处理**的文章里挑。
> 如果刚导入订阅、文章还没处理完，可能返回空 —— 先跑 4.5 确认有 `done` 的记录。

定时任务默认是每天 08:10（`AI_DIGEST_HOUR` / `AI_DIGEST_MINUTE`），由 `beat` 容器触发：

```bash
docker compose logs -f beat | grep -i digest
```

---

## 6. 稳定性观察（2 核 2GB）

```bash
docker stats --no-stream
```

**参考值**：合计常驻内存 900MB～1.2GB。若 `web` 或 `worker` 长期超过其 `mem_limit` 被 OOM kill，
优先把 `docker-compose.yml` 里的 `AI_BATCH_SIZE` 调小（默认 20）。

其他巡检命令：

```bash
docker compose exec worker celery -A rssant_ai.celery inspect registered  # 任务是否注册
docker compose exec worker celery -A rssant_ai.celery inspect active       # 运行中的任务
docker compose exec worker supervisorctl -c /app/docker/supervisord-worker.conf status
docker compose exec redis redis-cli info memory | grep used_memory_human
```

---

## 7. 已知会出现的无害日志

- `extension hstore already exists` —— 正常，`django_db_init` 每次启动都会尝试创建。
- `ai request ... timeout` 偶发 —— 客户端会退避重试，最终由巡查任务兜底。
- `digest item story_id=... not in candidates, skip` —— LLM 编造了不存在的 id，已被过滤（这是设计内的防御）。

---

## 8. 验收清单

- [ ] `docker compose build` 成功
- [ ] 5 个服务全部 Up 且健康
- [ ] `rssant_ai` 迁移已应用，3 张表存在
- [ ] 容器内静态冒烟通过（`docker/smoke_check.py`）
- [ ] 导入订阅后文章入库
- [ ] 文章有对应的 `StoryAI` 记录且 `status=done`
- [ ] `ai_summary` / `topic` / `ai_keywords` / `ai_score` 均已写入
- [ ] `/ai/` 页面能看到摘要与标签
- [ ] 真实 API 生成一次简报成功
- [ ] `docker stats` 内存在 1.2GB 以内

---

## 9. 真机部署实测踩到的坑（2026-10，Ubuntu 22.04 / 2C2G / 腾讯云）

这一节是上面所有静态验证都发现不了、只有真跑才会暴露的问题，已全部修复。

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | `docker compose build` 在 apt 阶段 16 分钟后失败，`libpq5 ... 404 Not Found` | **Debian 11 (bullseye) LTS 已于 2026-08-31 结束**，各镜像正在下架 `bullseye-security` 包池：索引还在、包文件已移走，全网 404 | main/updates 走阿里云镜像；**security 改走 `archive.debian.org`** 并 `Acquire::Check-Valid-Until "false"`（归档库 Release 已过期） |
| 2 | apt 卡住十几分钟无进展 | 云主机启用 IPv6 后 apt 优先走 v6，路由黑洞 | `Acquire::ForceIPv4 "true"` |
| 3 | 构建最后一步 `image "rssant-ai:latest": already exists` | web/worker/beat 三个服务并行 build **同一个 tag**，互相抢占 | 只在 `web` 保留 `build:`，其余服务只写 `image:` 引用 |
| 4 | `showmigrations rssant_ai` 显示 `(no migrations)`，三张表没建，但 `manage.py check` 全绿 | `rssant_ai/migrations/` **缺 `__init__.py`**，Django 不把它识别为迁移包，`migrate` 静默跳过 | 补上空的 `__init__.py` |
| 5 | 用 `.env` 里的密码登录失败 | 上游 `django_run_db_init` 先创建 `admin`/**`admin`**，本项目 `django_ensure_user` 见用户已存在就跳过，`.env` 密码永不生效 | 仅在口令仍是上游默认值 `admin` 时才同步成 `.env` 值，用户自己改过的密码不会被覆盖 |
| 6 | `celery-worker` 启动即 exit 2：`No such option: --queue` | Celery 5.3 的分发参数是 `-Q/--queues` | supervisord 里改成 `-Q ai` |
| 7 | `rssant-worker` exit 127：`/usr/bin/env: 'python\r': No such file or directory` | `runserver.py` 等工作区文件是 **CRLF**，shebang 被解析成 `python\r` | `.gitattributes` 全仓归一化为 LF + Dockerfile 对入口脚本再 `sed 's/\r$//'` 兜底 |

> 教训：`manage.py check`、离线 migration autodetector、容器内 import 冒烟这三样都**检查不出** #4/#6/#7 ——
> 前者只校验模型/迁移一致性，后两者走的是 Python API 而不是真实的 `migrate` / `celery` CLI / shebang 执行路径。
> 所以"能 import"不等于"能跑"，必须有一次真实 `up -d` + 真实抓取。

