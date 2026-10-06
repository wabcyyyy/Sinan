#!/usr/bin/env bash
# Sinan 单 VPS 部署/升级脚本（幂等）：拉码 → 构建 → 滚动拉起 → 冒烟门。
# 首次部署的前置步骤（装 Docker、写 .env、配 DNS）见 deploy/README.md。
# 用法：bash deploy/deploy.sh          # 常规部署/升级
#       SKIP_PULL=1 bash deploy/deploy.sh   # 本地改动直接部署（跳过 git pull）
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml --profile prod"

if [ ! -f .env ]; then
  echo "✗ 缺少根 .env（生产密钥清单见 deploy/README.md §环境变量）" >&2
  exit 1
fi
# DOMAIN 防呆：compose 侧对缺失取软默认 localhost（不炸常用命令），真部署在这里拦
DOMAIN="$(grep -E '^DOMAIN=' .env | head -1 | cut -d= -f2- | tr -d '\"'"'"' \r')"
if [ -z "$DOMAIN" ] || [ "$DOMAIN" = "localhost" ]; then
  echo "✗ .env 里没有 DOMAIN=你的域名（本机预演才用 localhost），Caddy 无法签发正式证书" >&2
  exit 1
fi
# 口令防呆：compose 兜底默认值（travel_dev_only / ta_dev_redis_only）随公开仓库知名，
# 漏配 .env 时生产会以公开口令静默起服——与 DOMAIN 一样在这里拦成显式失败。
for pair in "MYSQL_ROOT_PASSWORD:travel_dev_only" "REDIS_PASSWORD:ta_dev_redis_only"; do
  key="${pair%%:*}"
  fallback="${pair#*:}"
  val="$(grep -E "^$key=" .env | head -1 | cut -d= -f2- | tr -d '\"'"'"' \r')"
  if [ -z "$val" ] || [ "$val" = "$fallback" ]; then
    echo "✗ .env 缺 $key 或仍等于 compose 兜底默认值 $fallback（公开口令不能上生产，生成方式见 deploy/README.md §环境变量）" >&2
    exit 1
  fi
done
# 口令防呆第二层（BESEC-1）：仓库历史曾泄漏过一个 6 位纯数字口令且与开发库同值
# （docs/审查-上线前安全审计-2026-09-29.md P1-1，视该口令为已泄露）。这里不硬编码
# 泄漏值本身，直接给 MYSQL_ROOT_PASSWORD 设最低强度线——≥16 位且非纯数字，
# 泄漏值与同类弱口令一并拦下，部署者把它抄进生产 .env 时不再放行。
root_pwd="$(grep -E '^MYSQL_ROOT_PASSWORD=' .env | head -1 | cut -d= -f2- | tr -d '\"'"'"' \r')"
if [ "${#root_pwd}" -lt 16 ] || printf '%s' "$root_pwd" | grep -qE '^[0-9]+$'; then
  echo "✗ MYSQL_ROOT_PASSWORD 强度不足（要求 ≥16 位且非纯数字）：历史泄漏过的口令与弱口令不得用于任何环境，请用 openssl rand -hex 16 级别重新生成" >&2
  exit 1
fi
echo "▸ 目标站点：https://$DOMAIN"

if [ "${SKIP_PULL:-0}" != "1" ]; then
  echo "▸ 拉取最新代码"
  git pull --ff-only
fi

echo "▸ 构建并拉起（mysql redis agent-python caddy）"
$COMPOSE up -d --build

echo "▸ 等待后端健康（首启含建表迁移与 MySQL 初始化，最长 180s）"
deadline=$((SECONDS + 180))
until curl -fsS "http://127.0.0.1:8000/api/agent/health" >/dev/null 2>&1; do
  if [ $SECONDS -gt $deadline ]; then
    echo "✗ 后端 180s 未健康，看日志：docker logs ta-agent" >&2
    exit 1
  fi
  sleep 5
done
echo "  ✓ 后端健康"

echo "▸ 冒烟门（全部通过才算部署成功）"
# 1. 边缘静态：HTTPS 首页 200（顺带验证证书真的可被公开验证，失败会在这里暴露）
code="$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/")"
[ "$code" = "200" ] || { echo "✗ https://$DOMAIN/ 返回 $code（证书/DNS/防火墙 80·443 任一未通都会这样）" >&2; exit 1; }
echo "  ✓ 边缘静态 200"
# 2. 业务面经边缘可达
curl -fsS "https://$DOMAIN/api/test/hello" >/dev/null || { echo "✗ /api 反代不通" >&2; exit 1; }
echo "  ✓ /api 反代可达"
# 3. R1-8：agent 直调面绝不经边缘可达
code="$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/agent/health")"
[ "$code" = "403" ] || { echo "✗ /api/agent 经边缘返回 $code（应为 403，R1-8 被破坏）" >&2; exit 1; }
echo "  ✓ agent 面 403（R1-8 保持）"
# 4. Secure Cookie 防呆：叠加层漏加载时后端启动日志有明文 Cookie 告警
if docker logs ta-agent 2>&1 | grep -q "AUTH_COOKIE_SECURE=false"; then
  echo "✗ 检测到明文 Cookie 告警：deploy/docker-compose.prod.yml 叠加层没生效（命令少了 -f deploy/docker-compose.prod.yml？）" >&2
  exit 1
fi
echo "  ✓ 无明文 Cookie 告警"
# 5. Redis 活性：REDIS_URL 显式错值（如 example 曾显式设的 localhost:6380，compose 不覆盖
#    它）会让容器内 Redis 静默降级——吊销黑名单/登录锁退化为进程内兜底，站点照常起。
#    这里用真连接（ping）把它变成门禁可见，而不是等重启后吊销"复活"才暴露。
if ! $COMPOSE exec -T agent-python python -c "from app.common.redis_client import client; c = client(); assert c is not None and c.ping()"; then
  echo "✗ 后端连不上 Redis：查应用面 .env 的 REDIS_URL 是否显式设值（应删除该行走派生）或 ta-redis 是否健康" >&2
  exit 1
fi
echo "  ✓ Redis 连通"
# 6. 分享卡外壳（P1-5）：坏 token 也应返回注入前的原壳（200 HTML）——302/5xx 说明
#    FRONTEND_SHELL_URL 没注入或 /s/ 反代没配，分享链接在爬虫里没有卡片
if ! curl -fsS "https://$DOMAIN/s/smoke-invalid-token" | grep -q "<html"; then
  echo "✗ /s/ 分享外壳不可用：查 FRONTEND_SHELL_URL 与边缘 /s/* 反代" >&2
  exit 1
fi
echo "  ✓ /s/ 分享外壳可达"

echo "✓ 部署完成：https://$DOMAIN"
echo "  跟踪日志：docker compose -f docker-compose.yml logs -f agent-python"
