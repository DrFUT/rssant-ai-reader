# AI 智能信息流阅读器

> 基于开源项目 [rssant（蚁阅）](https://github.com/anyant/rssant) 二次开发的 **AI 增强型 RSS 信息流系统**。
> 自动聚合订阅源 → 大模型异步生成摘要 / 分类 / 评分 → 每天产出一份 AI 资讯简报。
> 面向 **2 核 2GB** 的低配自托管环境设计，一条 `docker compose up -d` 即可运行。

---

## 一、项目定位

原项目 rssant 是一个功能完整的 RSS 阅读器，但它解决的是「把文章收集起来」。
本项目要解决的问题是下一步：**信息太多，读不过来**。

| | 原 rssant | 本项目 |
|---|---|---|
| 信息获取 | RSS 订阅同步 | RSS 订阅同步（沿用） |
| 内容理解 | 无 | LLM 一句话总结 + 3 条关键观点 |
| 组织方式 | 按订阅源 | 按 AI 主题 + 标签 + 价值评分 |
| 每日回顾 | 无 | AI 每日资讯简报（含推荐原因） |
| 个性化 | 无 | 可解释的关键词兴趣过滤 |
| 部署形态 | 单容器（内置 nginx + postgres） | docker compose 五服务拆分 |

**一句话概括**：把一个 RSS 阅读器，改造成「会自动读、会自动挑、会自动总结」的信息流系统。

---

## 二、功能列表

### 1. RSS 订阅与阅读（沿用 rssant）
- 订阅源智能查找、OPML 导入导出
- 全文抓取、定时同步、内容去重
- 保留 rssant 原有的全部 API 与数据模型

### 2. AI 文章摘要（核心）
- **异步**生成，用户打开页面时绝不实时调用 LLM
- 输出结构化结果：一句话总结 / 3 条关键观点 / 关键词标签 / 价值评分（0-10）
- 失败自动重试，超过上限后标记为跳过，不会无限重试拖垮队列

### 3. AI 智能分类
- 一级主题：`AI / 技术 / 编程 / 商业 / 科学 / 娱乐 / 其他`
- 多标签关键词
- **分类不额外消耗 token**：主题由 LLM 顺带产出，本地规则做归一化与兜底

### 4. 每日 AI 简报
- Celery Beat 每天定时触发
- 读取最近 24 小时的高分文章 → LLM 精选 → 产出「今日概览 + 热点条目 + 推荐原因」
- 落库为结构化数据 + Markdown，可通过 API 或页面查看

### 5. 用户兴趣过滤（轻量实现）
- 配置关注关键词与关注主题
- 匹配是**可解释的加权规则**（标题 ×3、标签 ×2、摘要 ×1），不是黑盒推荐算法
- 没有配置 Key 或兴趣时不影响正常使用

### 6. 一键 Docker 部署
- 5 个服务：`database` / `redis` / `web` / `worker` / `beat`
- 首次启动自动建表、自动创建管理员账号

---

## 三、技术架构

### 3.1 架构图

```
                        ┌──────────────────────────────────────┐
                        │            浏览器 /ai/                │
                        │      零构建步骤的轻量阅读器页面        │
                        └──────────────────┬───────────────────┘
                                           │ REST (/api/v1/ai/*)
┌──────────────────────────────────────────▼───────────────────────────────┐
│  web 容器 (Django + gunicorn)                                             │
│  · REST API         · 阅读器页面        · 数据库迁移(仅此容器执行)         │
└──────────┬──────────────────────────────────────────┬────────────────────┘
           │ 读写                                      │ 派发异步任务
┌──────────▼──────────┐                    ┌──────────▼──────────────────┐
│  database           │                    │  redis (Celery broker)      │
│  PostgreSQL 13      │                    └──────────┬──────────────────┘
└──────────┬──────────┘                               │
           │                              ┌───────────▼──────────────────┐
           │                              │  beat 容器                    │
           │                              │  Celery Beat 定时调度         │
           │                              │  · 每 2 分钟   摘要巡查       │
           │                              │  · 每天 08:10  生成简报       │
           │                              └───────────┬──────────────────┘
           │                                          │ 投递任务
           │                              ┌───────────▼──────────────────┐
           │                              │  worker 容器                  │
           │                              │  ① Celery Worker (AI 任务)    │
           │                              │  ② rssant-worker (RSS 抓取)   │
           │                              │  ③ rssant-scheduler (调度)    │
           └──────────────────────────────┴───────────────────────────────┘
                                                      │
                                             ┌────────▼─────────┐
                                             │  LLM API         │
                                             │  DeepSeek/OpenAI │
                                             │  OpenAI 兼容接口  │
                                             └──────────────────┘
```

### 3.2 AI 处理流程

```
RSS 抓取文章
     │
     ▼
写入数据库 (story / story_volume)
     │
     ▼  ← 同步链路到此为止，绝不调用 LLM
创建 AI 待办记录 (StoryAI.status=pending) ──┐
     │                                      │
     ▼                                      │ 兜底：即使派发失败，
派发 Celery 任务（有上限）                   │ 巡查任务也会捞回来
     │                                      │
     ▼                                      │
Celery Worker 消费 ─────────────────────────┘
     │
     ├─ 读取正文 → 清洗 HTML → 截断（控制 token）
     ├─ 调用 LLM（超时 30s，指数退避重试 2 次）
     ├─ 解析 JSON（兼容 ```json 代码块、前后废话）
     ├─ 标签归一化 + 主题校验（规则兜底）
     └─ 写回数据库 (ai_summary / ai_points / ai_keywords / topic / ai_score)
```

**每日简报流程**

```
Celery Beat 每天 08:10 触发
     │
     ▼
取最近 24h、已处理、评分最高的 N 篇（只传标题/摘要/标签，不传正文）
     │
     ▼
一次 LLM 调用 → { overview, items:[{ story_id, digest, reason, score }] }
     │
     ▼
过滤模型编造的 story_id → 补全原文链接 → 落库 + 渲染 Markdown
```

### 3.3 目录结构（本次新增的部分）

```
ai/                          AI 服务层（纯 Python，不依赖 Django，可单测）
├── config.py                配置读取（.env / 环境变量，绝不硬编码 Key）
├── client.py                OpenAI 兼容 Chat Completions 客户端（超时+重试）
├── text.py                  HTML 清洗、截断、关键词边界匹配
├── prompts.py               提示词模板
├── summarizer.py            摘要：一句话 + 关键观点 + 标签 + 评分
├── classifier.py            分类：标签归一化 + 主题规则兜底
├── digest.py                每日简报生成与 Markdown 渲染
├── interest.py              兴趣匹配（可解释加权规则）
└── json_helper.py           LLM 输出 JSON 的稳健解析

rssant_ai/                   Django 集成层
├── models.py                StoryAI / DailyDigest / UserInterest
├── migrations/0001_initial.py
├── celery.py                Celery 应用 + Beat 定时任务表
├── tasks.py                 Celery 任务（薄封装，带软硬超时）
├── service.py               业务逻辑（唯一同时依赖 ORM 与 ai/ 的地方）
├── hooks.py                 RSS 同步完成后的挂载点
├── views.py                 REST 接口 + 阅读器页面
└── templates/rssant_ai/reader.html

docker/entrypoint.sh              容器入口（web / worker / beat / migrate）
docker/supervisord-worker.conf    worker 容器进程编排
docker-compose.yml                五服务编排 + 资源限制
Dockerfile                        去掉 node/nginx/postgres 的精简镜像
```

**对 rssant 原有代码的改动只有 4 处**（不到 20 行）：

| 文件 | 改动 |
|---|---|
| `rssant/settings/settings.py` | 注册 `rssant_ai` 应用；数据库配置不再按角色关闭；新增 Celery 配置 |
| `rssant/urls.py` | 挂载 `/ai/` 页面与 `/api/v1/ai/*` 接口 |
| `rssant_api/models/story_service.py` | 文章落库后调用 AI 钩子（3 行，try 包裹） |
| `requirements.txt` | 新增 celery / redis，Click 升到 8.x |

---

## 四、快速开始

### 4.1 准备配置

```bash
cp .env.example .env
# 至少改两处：
#   RSSANT_SECRET_KEY   随机字符串
#   AI_API_KEY          DeepSeek / OpenAI 的 Key（留空也能跑，只是没有 AI 增强）
```

### 4.2 启动

```bash
docker compose up -d
```

首次启动会自动完成：等待数据库 → 建表 → 初始化数据 → 创建管理员账号。
镜像构建约需 5～15 分钟（Python 3.8 的老依赖需要编译）。

### 4.3 使用

| 地址 | 说明 |
|---|---|
| http://localhost:6789/ai/ | AI 信息流阅读器（默认账号 `admin` / `change-me-please`） |
| http://localhost:6789/api/v1/ai/status | AI 运行状态与队列积压 |
| http://localhost:6789/admin/ | Django Admin（可查看 AI 摘要表） |

> 上面的 `change-me-please` 只是 `.env.example` 里的示例默认值。**部署到公网前**请在 `.env` 里把它改掉
> （`RSSANT_SUPERUSER_PASSWORD`），改完 `docker compose up -d` 生效。

订阅几个 RSS 源后，等待 1～2 分钟（或手动触发一次同步），AI 摘要就会出现在信息流里。

> 云服务器上访问不到页面，通常不是应用问题，而是安全组/防火墙没放行 `6789`
> （Docker 会自己写 iptables，云厂商的安全组在更外层）。腾讯云轻量需要单独加一条
> TCP:6789 的防火墙规则。
>
> 如果订阅是在 AI 功能上线前就已经导入的，旧文章不会自动补 AI 记录，需要跑一次：
>
> ```bash
> docker compose exec web python manage.py runscript ai_backfill
> ```

### 4.4 常用命令

```bash
docker compose ps                       # 查看服务状态
docker compose logs -f worker           # 看 AI 任务与 RSS 同步日志
docker compose logs -f beat             # 看定时调度
docker compose exec web python manage.py migrate
docker compose exec worker celery -A rssant_ai.celery inspect active   # 查看运行中的任务

# 容器内静态冒烟: 任务注册 / 低配参数 / AI 降级是否都正常
docker compose exec -T web python docker/smoke_check.py

# 端到端一键验收(只读, 不导入订阅不消耗额度)
bash docker/verify.sh
```

手动生成一次今日简报（同步执行，便于演示）：

```bash
TOKEN=$(curl -s -X POST http://localhost:6789/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"change-me-please"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

curl -s -X POST http://localhost:6789/api/v1/ai/digest/generate \
  -H "Authorization: Token $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"async":0}' | python3 -m json.tool
```

完整的逐项验收步骤见 [docs/VERIFY.md](docs/VERIFY.md)。

---

## 五、环境变量说明

### 5.1 AI 相关（核心）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AI_ENABLE` | `1` | 总开关，置 0 则完全不调用 LLM |
| `AI_API_KEY` | 空 | LLM 的 Key。**留空则降级为规则分类 + RSS 原文摘要，功能不残废** |
| `AI_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `AI_MODEL` | `deepseek-chat` | 模型名 |
| `AI_TIMEOUT` | `30` | 单次请求超时（秒） |
| `AI_BATCH_SIZE` | `20` | 每轮巡查最多处理多少篇（**防止任务堆积的关键阀门**） |
| `AI_DIGEST_HOUR` / `AI_DIGEST_MINUTE` | `8` / `10` | 每日简报生成时间（容器时区 Asia/Shanghai） |

换服务商只需改两个变量：

```bash
# DeepSeek
AI_BASE_URL=https://api.deepseek.com   AI_MODEL=deepseek-chat
# OpenAI
AI_BASE_URL=https://api.openai.com     AI_MODEL=gpt-4o-mini
# 月之暗面
AI_BASE_URL=https://api.moonshot.cn    AI_MODEL=moonshot-v1-8k
```

### 5.2 基础配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RSSANT_SECRET_KEY` | 开发默认值 | **生产必须修改**，服务间调用密钥由它派生 |
| `RSSANT_ROOT_URL` | `http://localhost:6789` | 对外访问地址 |
| `RSSANT_WEB_PORT` | `6789` | 宿主机端口 |
| `RSSANT_CHECK_FEED_MINUTES` | `30` | 订阅源检查间隔 |
| `RSSANT_SUPERUSER_USERNAME` | `admin` | 首次启动自动创建的管理员 |
| `RSSANT_SUPERUSER_PASSWORD` | `change-me-please` | 同上 |
| `RSSANT_PG_DB/USER/PASSWORD` | `rssant` | 数据库 |

完整列表见 `.env.example`。

---

## 六、REST API

所有接口位于 `/api/v1/ai/`，需要登录（Session 或 Token）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/status` | AI 是否启用、模型名、队列积压统计 |
| GET | `/stories` | 信息流。参数：`topic` `q` `min_score` `sort`(time/score/interest) `interest` `offset` `limit` |
| GET | `/story/<id>` | 单篇文章的 AI 结果 |
| POST | `/story/<id>/refresh` | 重新生成某篇的摘要 |
| GET | `/digest/latest` | 最新每日简报（加 `?markdown=1` 返回 Markdown） |
| GET | `/digest/<date>` | 指定日期简报 |
| POST | `/digest/generate` | 生成简报，`{"async":1}` 异步 / `{"async":0}` 同步 |
| GET/POST | `/interest` | 获取 / 设置兴趣配置 |
| GET | `/topics` | 各主题文章数 |

示例：

```bash
# 先取 Token(也可以用浏览器的登录 Session)
TOKEN=$(curl -s -X POST http://localhost:6789/api/v1/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"change-me-please"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

curl -s -H "Authorization: Token $TOKEN" \
  'http://localhost:6789/api/v1/ai/stories?topic=AI&sort=score&limit=5' \
  | python3 -m json.tool
```

---

## 七、低配（2 核 2GB）优化清单

这是本项目投入最多的部分，也是最能体现工程判断的地方：

| 优化项 | 做法 |
|---|---|
| Celery 并发 | 固定 `--concurrency=1`，单进程单并发，不与 Web 抢 CPU |
| 任务预取 | `--prefetch-multiplier=1` + `acks_late`，任务不会堆在 worker 内存 |
| 任务超时 | 每个任务都有软/硬超时（摘要 90/120s），LLM 挂住不会拖死 worker |
| 内存泄漏防护 | `--max-tasks-per-child=50`，子进程处理 50 个任务后回收 |
| 队列防堆积 | 巡查任务带 `expires=110s` + `batch_size` 硬上限，超时未消费直接丢弃 |
| 进程数量 | RSS 调度器、RSS 抓取器、Celery Worker 合并进同一个容器，减少常驻进程 |
| 去掉非必要服务 | 不启用图片代理服务（asyncapi），去掉 nginx 与容器内 postgres |
| Redis | 纯 broker 用途，关闭 AOF/RDB 持久化，限制 96MB |
| PostgreSQL | shared_buffers 128MB、max_connections 60、连接池收窄到 5+10 |
| 容器资源 | 每个服务都设 `mem_limit` / `cpus` 上限 |
| LLM 成本控制 | 摘要与分类合并为一次调用；正文截断；简报只传标题与摘要不传正文 |
| 失败降级 | 无 Key / 调用失败 → 规则分类 + 原文摘要，系统照常可用 |

实测常驻内存约 900MB～1.1GB（含 PostgreSQL 与 Redis）。

---

## 八、项目亮点（面试可讲的点）

1. **异步优先的 AI 架构**：LLM 调用完全不在用户请求链路里。从「文章落库 → 建待办 → 派发任务 → 消费 → 写回」是一条带状态机的流水线，有 pending/processing/done/failed/skipped 五种状态、重试计数和僵尸任务回收。

2. **双保险的任务触发**：入库即派发保证低延迟；2 分钟一次的巡查任务保证不丢单。派发失败、Redis 抖动都不影响最终一致性。

3. **为省钱而做的提示词设计**：摘要 + 分类合并成一次 LLM 调用，分类结果由本地规则归一化；每日简报只传标题与摘要不传正文，token 消耗降到最低。

4. **对 LLM 输出的不信任设计**：兼容 ```json 代码块、前后废话、字段名漂移；简报会过滤掉模型编造的 `story_id`；评分做区间收敛。

5. **没有 Key 也能跑**：降级为规则分类 + RSS 原文摘要，功能不残废 —— 对自托管产品很重要。

6. **极小的侵入式改造**：对上游 rssant 只改了 4 个文件、不到 20 行，RSS 核心能力一行没动。AI 逻辑集中在 `ai/`（纯 Python，可单测）与 `rssant_ai/`（Django 集成）两层。

7. **真实约束下的取舍**：明确放弃「复杂推荐算法」，选择可解释的加权规则；明确放弃「官方前端」（上游未开源），自己写零构建步骤的页面 —— 都是在资源与工期约束下的主动降级。

---

## 九、技术栈

| 层 | 选型 |
|---|---|
| 语言 / 框架 | Python 3.8、Django 2.2、Django REST Framework |
| 异步任务 | Celery 5.3 + Redis（Broker & Result Backend） |
| 定时调度 | Celery Beat |
| 数据库 | PostgreSQL 13 |
| 部署 | Docker / Docker Compose（5 服务） |
| LLM | DeepSeek / OpenAI 及任何 OpenAI 兼容接口（HTTP 直连，不依赖官方 SDK） |

> 技术栈沿用上游 rssant（Python 3.8 + Django 2.2）。升级 Django 大版本会破坏上游大量
> 已停止维护的依赖（django-allauth 0.44、django-rest-swagger、django-seal 等），
> 在「能运行 > 架构漂亮」的前提下选择保持不动。

---

## 十、与原项目 rssant 的关系

- 上游：[github.com/anyant/rssant](https://github.com/anyant/rssant)（BSD 3-Clause，作者 guyskk），基于 commit `81c648c`
- RSS 抓取、订阅管理、全文提取、数据模型等核心能力全部沿用，未做改动
- 上游前端 `box/web` 未开源（已 gitignore），因此本项目自带一个零构建步骤的轻量阅读器页面
  （上游 README 已移至 `docs/README-rssant-upstream.md`）
- 上游是「单容器 + nginx + 内置 postgres + supervisord」的部署形态，本项目改为 compose 五服务拆分

---

## 十一、常见问题

**Q：没有 AI_API_KEY 能用吗？**
能。系统会降级为「规则分类 + RSS 原文摘要」，阅读、订阅、兴趣筛选全部照常工作，只是没有 LLM 增强的总结。

**Q：为什么文章没有立刻出现 AI 摘要？**
摘要是异步生成的。入库即派发任务，最慢兜底是 2 分钟一次的巡查任务。可在 `/api/v1/ai/status` 看队列积压。

**Q：构建很慢 / 失败？**
镜像基于 `python:3.8.12-bullseye`，部分老依赖需要本地编译。可切换 PyPI 镜像：
`docker compose build --build-arg PYPI_MIRROR=https://pypi.org/simple`

**Q：想换 LLM 服务商？**
改 `.env` 里的 `AI_BASE_URL` 与 `AI_MODEL` 即可，代码无需改动（均为 OpenAI 兼容协议）。

---

## License

BSD 3-Clause License，继承上游 rssant 的许可证，版权归原作者 guyskk 所有。
本次 AI 增强部分的改动同样遵循该许可证。
