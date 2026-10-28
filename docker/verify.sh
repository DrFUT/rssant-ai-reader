#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 端到端验收脚本(在宿主机上跑, 需要 python3)
#
#   bash docker/verify.sh                 # 只做只读检查
#   bash docker/verify.sh --import-feed   # 顺带导入两个示例订阅源
#   bash docker/verify.sh --digest        # 顺带用真实 API 同步生成一次简报
#
# 它会依次验证:
#   服务状态 -> 迁移/表 -> 登录 -> AI 状态 -> 文章入库 -> AI 摘要写回
#   -> 简报 -> 资源占用
# ---------------------------------------------------------------------------
set -uo pipefail

# 仓库根目录若有 .env, 直接复用里面的账号/库名, 省得手工再填一遍
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

BASE_URL="${RSSANT_ROOT_URL:-http://localhost:6789}"
USERNAME="${RSSANT_SUPERUSER_USERNAME:-admin}"
PASSWORD="${RSSANT_SUPERUSER_PASSWORD:-admin123456}"
PG_USER="${RSSANT_PG_USER:-rssant}"
PG_DB="${RSSANT_PG_DB:-rssant}"

DO_IMPORT=0
DO_DIGEST=0
for arg in "$@"; do
  case "$arg" in
    --import-feed) DO_IMPORT=1 ;;
    --digest) DO_DIGEST=1 ;;
  esac
done

# 后续所有入库查询都走这一个入口, 库名/用户名只写一次
PSQL=(docker compose exec -T database psql -U "$PG_USER" -d "$PG_DB")

ok()   { echo "  [PASS] $*"; }
bad()  { echo "  [FAIL] $*"; FAILED=$((FAILED + 1)); }
info() { echo "         $*"; }
FAILED=0

jqr() { python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('<non-json>'); sys.exit(0)
expr = sys.argv[1]
try:
    print(eval(expr, {'d': d, 'len': len, 'str': str}))
except Exception as e:
    print('<missing>')
" "$1"; }

echo "== 1. 服务状态 =="
docker compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null | while read -r line; do
  echo "  $line"
done
for svc in database redis web worker beat; do
  st=$(docker compose ps --format '{{.Status}}' "$svc" 2>/dev/null | head -1)
  if echo "$st" | grep -q "Up"; then ok "$svc 已启动 ($st)"; else bad "$svc 未启动 ($st)"; fi
done

echo "== 2. 数据库迁移与表 =="
tables=$("${PSQL[@]}" -tAc \
  "select count(*) from information_schema.tables where table_name like 'rssant_ai%'" 2>/dev/null | tr -d ' ')
if [ "$tables" = "3" ]; then ok "rssant_ai 三张表已创建"; else bad "rssant_ai 表数量=$tables (期望 3)"; fi
applied=$(docker compose exec -T web python manage.py showmigrations rssant_ai 2>/dev/null | grep -c '\[X\]')
if [ "$applied" -ge 1 ]; then ok "rssant_ai 迁移已应用 (${applied} 个)"; else bad "rssant_ai 迁移未应用"; fi

echo "== 3. 登录 =="
LOGIN=$(curl -s -X POST "${BASE_URL}/api/v1/auth/login/" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"${USERNAME}\",\"password\":\"${PASSWORD}\"}") || bad "登录请求失败"
echo "$LOGIN" > /tmp/_rssant_login.json
TOKEN=$(echo "$LOGIN" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("key",""))' 2>/dev/null)
if [ -n "$TOKEN" ]; then ok "登录成功, 已取得 Token"; else bad "登录失败: $(echo "$LOGIN" | head -c 200)"; fi
AUTH=(-H "Authorization: Token ${TOKEN}")

echo "== 4. AI 状态 =="
STATUS=$(curl -s "${AUTH[@]}" "${BASE_URL}/api/v1/ai/status")
ENABLED=$(echo "$STATUS" | jqr "d.get('enabled')")
MODEL=$(echo "$STATUS" | jqr "d.get('model')")
QUEUE=$(echo "$STATUS" | jqr "d.get('queue')")
info "enabled=$ENABLED model=$MODEL"
info "queue=$QUEUE"
[ "$ENABLED" = "True" ] && ok "AI 已启用" || info "AI 未启用(无 Key, 将走规则降级, 不影响其它验收)"

if [ "$DO_IMPORT" = "1" ]; then
  echo "== 5. 导入订阅源 =="
  curl -s -X POST "${BASE_URL}/api/v1/feed.import" "${AUTH[@]}" \
    -H 'Content-Type: application/json' \
    -d '{"text":"https://www.ruanyifeng.com/blog/atom.xml\nhttps://feeds.appinn.com/appinns/"}' \
    > /tmp/_rssant_import.json
  info "$(head -c 300 /tmp/_rssant_import.json)"
  ok "导入请求已发送, 请等待 1~3 分钟让 worker 抓取"
else
  echo "== 5. 导入订阅源 =="
  info "跳过(加 --import-feed 启用)"
fi

echo "== 6. 文章与 AI 摘要 =="
STORY_N=$("${PSQL[@]}" -tAc \
  'select count(*) from rssant_api_storyinfo' 2>/dev/null | tr -d ' ')
info "文章总数(story) = ${STORY_N}"
if [ "${STORY_N:-0}" -gt 0 ]; then ok "文章已入库"; else bad "文章未入库, 请先导入订阅并等待抓取"; fi
"${PSQL[@]}" -c \
  'select status, count(*) from rssant_ai_storyai group by status order by 1' 2>/dev/null | sed 's/^/  /'
DONE_N=$("${PSQL[@]}" -tAc \
  "select count(*) from rssant_ai_storyai where status='done'" 2>/dev/null | tr -d ' ')
SKIP_N=$("${PSQL[@]}" -tAc \
  "select count(*) from rssant_ai_storyai where status='skipped'" 2>/dev/null | tr -d ' ')
# 判定口径: done = 已产出结果(LLM 或 fallback 降级都算, 看 model 列区分);
# skipped = 主动跳过(正文为空 / 文章不存在 / 超过重试上限)。
RESULT_N=$(( ${DONE_N:-0} + ${SKIP_N:-0} ))
if [ "${RESULT_N:-0}" -gt 0 ]; then
  ok "AI 结果已写回 ${RESULT_N} 篇 (done=${DONE_N:-0}, 主动跳过=${SKIP_N:-0})"
else
  bad "尚无任何 AI 结果(done=${DONE_N:-0}, skipped=${SKIP_N:-0})"
fi
"${PSQL[@]}" -c \
  "select id, topic, ai_score, status, model, left(ai_summary, 30) as summary from rssant_ai_storyai where status in ('done','skipped') order by id desc limit 5" \
  2>/dev/null | sed 's/^/  /'

echo "== 7. 信息流接口 =="
curl -s "${AUTH[@]}" "${BASE_URL}/api/v1/ai/stories?limit=2&sort=score" \
  | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("         <非 JSON 响应>")
    raise SystemExit(1)
stories = d.get("stories") or []
print("         total=%s 返回 %s 条" % (d.get("total"), len(stories)))
for s in stories[:2]:
    print("         - [%s] %s/10 %s" % (s.get("topic"), s.get("ai_score"), (s.get("title") or "")[:36]))
    print("           摘要: %s" % ((s.get("ai_summary") or "")[:50]))
if not stories:
    print("         (暂无文章: 订阅刚导入, 等 worker 抓取完成后再看)")
' || bad "信息流接口调用失败"

if [ "$DO_DIGEST" = "1" ]; then
  echo "== 8. 生成每日简报(真实 API, 同步) =="
  curl -s -X POST "${AUTH[@]}" "${BASE_URL}/api/v1/ai/digest/generate" \
    -H 'Content-Type: application/json' -d '{"async":0}' > /tmp/_rssant_digest.json
  python3 -c '
import json
d = json.load(open("/tmp/_rssant_digest.json"))
if "date" not in d:
    print("  [FAIL] 简报生成失败:", str(d)[:200]); raise SystemExit(0)
items = d.get("items") or []
print("  [PASS] %s 简报生成成功, 候选 %s 篇, 入选 %s 条, 模型 %s"
      % (d["date"], d.get("story_count"), len(items), d.get("model")))
print("         概览:", (d.get("overview") or "")[:70])
for i, it in enumerate(items[:5], 1):
    print("         %s. %s | %s/10 | %s"
          % (i, (it.get("title") or "")[:38], it.get("score"), it.get("reason")))
'
else
  echo "== 8. 生成每日简报 =="
  info "跳过(加 --digest 启用, 需要配置真实 AI_API_KEY)"
fi

echo "== 9. 资源占用 =="
docker stats --no-stream --format '  {{.Name}}  CPU {{.CPUPerc}}  MEM {{.MemUsage}}' 2>/dev/null

echo
if [ "$FAILED" -eq 0 ]; then echo "验收通过 ✅"; else echo "有 ${FAILED} 项未通过 ❌ (详见上方 [FAIL])"; fi
exit "$FAILED"
