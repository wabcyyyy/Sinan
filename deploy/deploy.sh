#!/usr/bin/env bash
# Sinan 单 VPS 部署/升级脚本（幂等）：拉码 → 构建 → 滚动拉起 → 冒烟门。
# 首次部署的前置步骤（装 Docker、写 .env、配 DNS）见 deploy/README.md。
# 用法：bash deploy/deploy.sh                    # 常规部署/升级（默认拉 master）
#       DEPLOY_REF=<sha> bash deploy/deploy.sh   # 检出指定 commit 部署（CI 门禁验证过的
#                                                 # 那棵树，deploy.yml 经 workflow_run 传入）
#       SKIP_PULL=1 bash deploy/deploy.sh        # 本地改动直接部署（跳过拉码）
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml --profile prod"

# 读取运行密钥的【生效值】（P0-5 部署侧防呆用）：agent-python 容器的 env_file 顺序是
# [travel-agent-python/.env, ./.env]（docker-compose.yml），同名键后者覆盖前者——
# 防呆必须按同一优先级取值，否则根 .env 把占位串覆盖进去时这里会漏拦。
effective_env() {
  local key="$1" val=""
  # 每条管道带 || true：键不存在时 grep 以 1 退出，set -o pipefail 下裸赋值会把
  # 本函数（乃至整个脚本）静默打死——防呆函数自己必须永不失败
  if [ -f travel-agent-python/.env ]; then
    val="$(grep -E "^$key=" travel-agent-python/.env | head -1 | cut -d= -f2- | tr -d "\"' \r" || true)"
  fi
  if grep -Eq "^$key=" .env; then
    val="$(grep -E "^$key=" .env | head -1 | cut -d= -f2- | tr -d "\"' \r" || true)"
  fi
  printf '%s' "$val"
}

# 密钥防呆（审计 §3.4.2 / P0-5）：.env.example 的占位串是公开的——JWT_SECRET 的
# replace-with-…-at-least-32-chars 恰好 51 字符，能穿过启动校验的 ≥32 下限直上生产，
# 误配 = 任何人可离线伪造含 admin 的会话票；AGENT_INTERNAL_TOKEN 与 LLM_API_KEY 的
# 占位串同理（前者让 agent 面共享令牌人尽皆知，后者上线即全量生成失败）。与上面的
# DOMAIN/口令防呆同一姿态：占位串或空值在这里拦成显式失败，不带病上线。
# LLM key 的「真实调用探针」（1-token 生成冒烟）不在部署脚本层做，另见审计 P0-5。
reject_secret() {
  local key="$1" val="$2" placeholder="$3" hint="$4"
  if [ -z "$val" ]; then
    echo "✗ $key 未配置（$hint）" >&2
    exit 1
  fi
  case "$val" in
    "$placeholder"*)
      echo "✗ $key 仍是 .env.example 的占位串（$placeholder…），公开值不能上生产（$hint）" >&2
      exit 1
      ;;
  esac
}

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
# 运行密钥防呆（P0-5）：三个键按容器实际生效值（见 effective_env）拦占位串与空值
reject_secret JWT_SECRET "$(effective_env JWT_SECRET)" replace-with "openssl rand -hex 32 生成，见 deploy/README.md §应用面"
reject_secret AGENT_INTERNAL_TOKEN "$(effective_env AGENT_INTERNAL_TOKEN)" replace-with "openssl rand -hex 32 生成，见 deploy/README.md §应用面"
reject_secret LLM_API_KEY "$(effective_env LLM_API_KEY)" your- "生成链路的真实成本来源，空值/占位串上生产即全量生成失败"
# JWT_SECRET 强度线（镜像 MYSQL_ROOT_PASSWORD 的第二层防呆）：启动校验只看长度 ≥32，
# 占位串被拦后，纯数字/纯字母长串是剩下最近的弱形态，一并在这里拦下。
jwt_secret="$(effective_env JWT_SECRET)"
if [ "${#jwt_secret}" -lt 32 ] || printf '%s' "$jwt_secret" | grep -qE '^[0-9]+$'; then
  echo "✗ JWT_SECRET 强度不足（要求 ≥32 字符且非纯数字）：请用 openssl rand -hex 32 重新生成" >&2
  exit 1
fi
echo "▸ 目标站点：https://$DOMAIN"

if [ "${SKIP_PULL:-0}" != "1" ]; then
  if [ -n "${DEPLOY_REF:-}" ]; then
    echo "▸ 检出指定 commit（CI 门禁验证过的那棵树）：$DEPLOY_REF"
    git fetch origin
    # -B 保持在 master 分支上重置到目标 commit（不留 detached HEAD），下次常规
    # 部署的 git pull --ff-only 仍可直接工作；防「CI 绿的是 A、拉码拉到 B」两棵树
    git checkout -q -B master "$DEPLOY_REF"
  else
    echo "▸ 拉取最新代码"
    git pull --ff-only
  fi
fi

# 失败回滚（P0-1）：先把当前运行中的镜像打保留 tag 再上新版；180s 健康门失败时
# 自动回退到保留 tag 并以非零退出。镜像级回退——源码树不回退（迁移 append-only，
# 代码回滚按前向兼容设计，手动路径见 README），下次部署照常重建。
# compose 对只有 build: 的服务按 <project>-<service> 命名镜像（name: travel-assistant），
# 回退 = 把保留 tag 重打回 latest，再 --no-build 重建容器。
AGENT_IMAGE="travel-assistant-agent-python"
CADDY_IMAGE="travel-assistant-caddy"
KEEP_TAG="rollback-keep"
prev_agent_image="$(docker inspect --format '{{.Image}}' ta-agent 2>/dev/null || true)"
prev_caddy_image="$(docker inspect --format '{{.Image}}' ta-caddy 2>/dev/null || true)"
if [ -n "$prev_agent_image" ]; then
  echo "▸ 保留当前运行镜像（健康门失败时回退用）：$KEEP_TAG"
  docker tag "$prev_agent_image" "$AGENT_IMAGE:$KEEP_TAG"
  [ -z "$prev_caddy_image" ] || docker tag "$prev_caddy_image" "$CADDY_IMAGE:$KEEP_TAG"
fi

echo "▸ 构建并拉起（mysql redis agent-python caddy）"
$COMPOSE up -d --build

# 等后端健康：curl 通 /api/agent/health（容器网络内回环直探，不经边缘）。首启含
# 建表迁移与 MySQL 初始化最长 180s；回滚后复用同一等待确认旧版真的起来了。
wait_backend_healthy() {
  local stage="$1" deadline=$((SECONDS + 180))
  until curl -fsS "http://127.0.0.1:8000/api/agent/health" >/dev/null 2>&1; do
    if [ $SECONDS -gt $deadline ]; then
      echo "✗ $stage：后端 180s 未健康，看日志：docker logs ta-agent" >&2
      return 1
    fi
    sleep 5
  done
}

echo "▸ 等待后端健康（首启含建表迁移与 MySQL 初始化，最长 180s）"
if ! wait_backend_healthy "新版启动"; then
  if [ -z "$prev_agent_image" ]; then
    echo "✗ 首启部署没有旧版本可回退，站点不可用——修复后重跑部署" >&2
    exit 1
  fi
  echo "▸ 健康门失败，回滚到部署前镜像（$KEEP_TAG → latest）"
  docker tag "$AGENT_IMAGE:$KEEP_TAG" "$AGENT_IMAGE:latest"
  rollback_services="agent-python"
  if [ -n "$prev_caddy_image" ]; then
    docker tag "$CADDY_IMAGE:$KEEP_TAG" "$CADDY_IMAGE:latest"
    rollback_services="$rollback_services caddy"
  fi
  $COMPOSE up -d --no-build --force-recreate $rollback_services
  wait_backend_healthy "回滚版启动" ||
    echo "✗ 回滚版也未健康：旧镜像也起不来时大概率是环境问题（MySQL/Redis/.env），需人工介入" >&2
  echo "✗ 部署失败，已回退到上一版镜像（源码树仍在最新 commit）；以非零退出，请查 ta-agent 日志定位新版问题" >&2
  exit 1
fi
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
