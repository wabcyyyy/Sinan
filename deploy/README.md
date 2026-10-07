# 司南生产部署（单 VPS + Docker Compose + Caddy）

单台 VPS 跑全栈：MySQL + Redis + FastAPI 后端 + Caddy 边缘（自动 HTTPS、静态托管、`/api` 反代三合一）。
一次部署 = 一条命令；升级 = 同一条命令；部署简单性是产品叙事的一部分，文档按"从零到上线"顺序写。

## 架构

```
浏览器
  │  https://<DOMAIN>  （Caddy 自动签发/续期 Let's Encrypt 证书，80 强制跳 443）
  ▼
┌────────────────────── VPS（Docker Compose）──────────────────────┐
│ caddy (ta-caddy)                                                 │
│   ├─ /          → 静态资源（React 构建产物）+ SPA fallback        │
│   ├─ /api/*     → agent-python:8000（XFF 替换，SSE 不缓冲）       │
│   └─ /api/agent/* /mcp* → 403（R1-8：agent 直调面绝不经边缘可达）  │
│ agent-python (ta-agent)  ── 业务面 + agent 面同进程              │
│   ├─ mysql (ta-mysql)    用户数据；启动时自动跑 V*.sql 迁移        │
│   └─ redis  (ta-redis)   JWT 吊销黑名单 / 登录锁 / 缓存（带口令）  │
└──────────────────────────────────────────────────────────────────┘
```

服务拓扑的单一真源是根 `docker-compose.yml`（`--profile prod`）；`deploy/docker-compose.prod.yml`
只放生产专属覆盖（目前仅 `AUTH_COOKIE_SECURE=true`）；路由语义与本地演示链路的
`travel-frontend-react/nginx.conf` 对齐（两处互为镜像，改动必须同步，见 Caddyfile 头注）。

## 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| VPS | ≥2GB 内存，Ubuntu 24.04，x86_64 | 1GB 也能跑但 MySQL 常驻 ~500MB，建议 2GB；另加 2GB swap 兜底 |
| 域名 | 一个 A 记录指到 VPS IP | 证书由 Caddy 自动向 Let's Encrypt 签发；没有域名见附录 A |
| 防火墙 | 放行 80/tcp、443/tcp+udp、22 | 443/udp 是 HTTP/3，不放行只是降级 HTTP/2，不报错 |
| Docker | Docker Engine + compose 插件 | `curl -fsSL https://get.docker.com \| sh` 一条命令装齐 |

## 首次部署（从零到上线）

```bash
# 1. 装 Docker（VPS 上）
curl -fsSL https://get.docker.com | sh

# 2. 拉代码（目录名 Sinan 与 .github/workflows/deploy.yml 的 VPS_DEPLOY_DIR 默认值
#    一致；旧 VPS 已按 Travel-Assistant 建目录的不必搬，配 GitHub Variable
#    VPS_DEPLOY_DIR 指向旧路径即可）
git clone <你的仓库地址> Sinan && cd Sinan

# 3. 写环境变量（两份，职责不同；都在 .gitignore 里，绝不入库）
cp .env.compose.example .env                      # compose 面：基础设施口令 + DOMAIN
cp travel-agent-python/.env.example travel-agent-python/.env   # 应用面：LLM key 等
```

**根 `.env` 必填**（compose 面）：

| 键 | 示例/生成方式 | 说明 |
|---|---|---|
| `DOMAIN` | `travel.example.com` | 站点域名单一来源，Caddy 据此签证书；缺失时软默认 `localhost`，但 `deploy.sh` 会拒绝在非 localhost 域名缺席时部署 |
| `MYSQL_ROOT_PASSWORD` | `openssl rand -hex 16` | **必须换掉默认值**（示例值是公开的） |
| `REDIS_PASSWORD` | `openssl rand -hex 16` | **必须换掉默认值**——Redis 里存 JWT 吊销黑名单与登录锁（R1-7） |
| `AUTH_COOKIE_SECURE` | `true` | 冗余保险：叠加层已强制，双保险防误删 |

**`travel-agent-python/.env` 必填**（应用面）：

| 键 | 说明 |
|---|---|
| `JWT_SECRET` | `openssl rand -hex 32`；<32 字符启动直接拒绝（签发会话票的密钥） |
| `AGENT_INTERNAL_TOKEN` | `openssl rand -hex 32`；非回环绑定时不配启动直接拒绝 |
| `LLM_API_KEY`（及 `LLM_BASE_URL`/`LLM_MODEL`） | 生成链路的真实成本来源 |
| `AGENT_DEADLINE_SECONDS=300` | 默认 90s 在供应商慢时整趟生成跑不完（本机实测 274s），生产同建议 300 |
| 可选 key | `OTM_API_KEY` / `UNSPLASH_ACCESS_KEY` / `PEXELS_ACCESS_KEY` / `TRAVELPAYOUTS_TOKEN` / `SERPAPI_KEY`——留空只降级对应能力，不影响启动 |
| `UA_CONTACT` | **建议填写**：外呼统一 User-Agent 的联系方式（Nominatim/Wiki 等免费源政策要求可联系的 UA）。全仓五处外呼 UA 已收拢单一工厂（BESEC-2），改这一处即全量生效；留空用开发占位地址，上生产前务必换成真实邮箱 |
| `ITEM_FEEDBACK_ENABLED=true` | 推荐开启：条目对/错反馈（C3.5）的能力开关，关闭时详情页自动隐藏反馈控件（探针 404） |
| `REDIS_URL` 保持注释 | **不要显式设值**：compose 只注入 `REDIS_HOST/PORT/PASSWORD`、不覆盖 URL，显式设 `localhost:6380` 会让容器内 Redis 静默降级（冒烟第 5 项会拦）；保持注释走派生即可 |

不需要动 `TRUSTED_PROXIES`：compose 默认值已含 `172.16.0.0/12`，覆盖 Caddy 所在的
compose 网络；XFF 由 Caddy **替换**后传入（`deploy/Caddyfile`），归因取到的就是真实客户端 IP，
按 IP 的登录锁/分享限速才有效。

```bash
# 4. 部署（构建 + 拉起 + 冒烟门；以后每次升级也是这一条）
bash deploy/deploy.sh
```

冒烟门五项全过才算成功：边缘静态 200、`/api` 反代可达、`/api/agent` 403（R1-8）、
无明文 Cookie 告警（叠加层漏加载的防呆）、Redis 连通（真 ping——REDIS_URL 显式错值会让
吊销黑名单/登录锁静默降级为进程内，站点照常起，只有这里能拦住）。首启含建表迁移与 MySQL 初始化，最长等 180s。
部署前预检同拦公开口令与占位密钥：`MYSQL_ROOT_PASSWORD` / `REDIS_PASSWORD` 缺失或仍等于
compose 兜底默认值（`travel_dev_only` / `ta_dev_redis_only`）时拒绝部署；`JWT_SECRET` /
`AGENT_INTERNAL_TOKEN`（`replace-with` 前缀占位串，JWT 还要求 ≥32 字符且非纯数字）与
`LLM_API_KEY`（`your-` 前缀占位串）缺配或仍是 `.env.example` 公开占位值时同样拒绝——
占位 JWT 密钥上产等于任何人可伪造会话票（审计 §3.4.2 / P0-5）。

上线后打开 `https://<DOMAIN>` 注册账号即可（dev 库的测试账号不会带到生产）。

## 日常运维

```bash
# 升级：拉新码 → 重建镜像 → 滚动替换（同一条命令，幂等）
bash deploy/deploy.sh

# 回滚代码（数据库迁移是 append-only，不自动回退；代码回滚按前向兼容设计）
git checkout <上一个可用 tag 或 sha>
SKIP_PULL=1 bash deploy/deploy.sh
# ↑ checkout 后是 detached HEAD：下次升级前先 git checkout master && git reset --hard origin/master

# 自动回滚（P0-1）：deploy.sh 每次部署前会把当前运行镜像打 rollback-keep 保留 tag，
# 后端 180s 健康门失败时自动把容器回退到该镜像并以非零退出（源码树不回退——
# 镜像级止损，下次部署照常重建；上面 git checkout 是代码级手动回滚）。

# 看状态 / 日志
docker compose -f docker-compose.yml --profile prod ps
docker compose -f docker-compose.yml logs -f agent-python
docker stats
```

升级也可以全自动：`.github/workflows/deploy.yml` 不再由 push 直接触发，而是在 `ci` 工作流
成功结束后经 workflow_run 触发（CI 未通过不得部署，P0-1），并锁定部署 ci 实际验证过的
commit（防「CI 绿的是 A、拉码拉到 B」）；手动 workflow_dispatch 保留给运维应急。需配
Secrets `VPS_HOST`/`VPS_USER`/`VPS_SSH_KEY` 与 Variables `VPS_DEPLOY_ENABLED=1`，可选
`VPS_DEPLOY_DIR`（默认 `/root/Sinan`，与首次部署的 clone 目录一致）。首次部署仍是手动的，
自动化只接管日常升级。

### 备份

备份/恢复不在这里另立口径——单一工具是 `travel-agent-python/scripts/backup_restore.py`
（纯 stdlib，VPS 裸 python3 即可跑；bundle 结构、恢复约束与演练记录见 README「运维（自托管）」节）。
它经 `docker compose exec` 进容器，与 prod profile 天然兼容。需要备份的只有 MySQL 与
`travel-agent-python/data/`（上传图、PDF 导出、轨迹与用量库），工具一条命令打包齐。

```bash
# 备份前先停写入方（MySQL 保持运行——工具本身不启停服务）
docker compose -f docker-compose.yml --profile prod stop agent-python
python3 travel-agent-python/scripts/backup_restore.py backup --out /var/backups/sinan-$(date +%F)
docker compose -f docker-compose.yml --profile prod start agent-python
```

Redis **不备份**：里面存的是会话态——活跃 JWT 票根（`auth:sess:tok:{jti}`，**明文**，与
登录返回的 token 逐字节一致，拿到 Redis 读权限即可冒充在线用户，BESEC-3 遗留）、吊销黑名
单、登录锁与限速桶。丢了自动重建——代价是全部在线会话与被吊销的票最多 24h 内复活（JWT
有效期），个人产品可接受，别为它加复杂度；正因票根明文在内，Redis 口令与 127.0.0.1 绑定
两道闸（compose + deploy.sh 预检）都不能松。

## 附录 A：暂无域名

两个选择，按省钱顺序：

1. **免费子域名走正规 HTTPS（推荐）**：DuckDNS 等免费子域名，A 记录指到 VPS，`DOMAIN`
   填它，其余流程完全不变——Let's Encrypt 对这类域名照常签发。
2. **纯 IP 裸 HTTP**：根 `.env` 里 `DOMAIN=:80`，且**不用** prod 叠加层（否则 Secure Cookie
   在明文链路上被浏览器丢弃，无法登录）：
   `docker compose --profile prod up -d --build`。会话票明文传输，只可接受于临时演示。

## 附录 B：本机预演（买 VPS 前先跑通一遍）

开发机上可直接预演整条生产链路（验证的是编排、路由与冒烟门，不是真 TLS）：

```bash
# 前提：先 stop-all 停掉本地活栈（预演要占用 8000/80/443 端口）
# 根 .env：DOMAIN=localhost，两份 .env 按"首次部署"写好（key 可用开发机的）
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml --profile prod up -d --build
```

Caddy 对 `localhost` 签内部自签证书：浏览器访问 `https://localhost` 需信任其根证书
（或用 `curl -k`）。预演通过后清掉：`docker compose --profile prod down`。

## 设计注记（为什么长这样）

- **Caddy 而不是 nginx+certbot**：证书签发续期零配置零 cron，单二进制 ~30MB；TLS 自动化
  是"部署简单性"叙事的最大头。
- **静态托管与 `/api` 反代同进程**：比"nginx 容器 + certbot 容器 + 前端容器"少一跳少两容器，
  SSE 也不多过一层缓冲语义。
- **agent 面与 `/mcp` 在边缘 403**（R1-8）：agent 直调面接受调用方自备 user_id 且只做
  X-Agent-Token 校验，从公网它就是匿名烧钱口；只允许容器网络内直连 + 环绕边缘的调用方式。
- **`flush_interval -1`**：对齐后端 SSE 响应自带 `X-Accel-Buffering: no` 的意图，生成进度
  事件即时下发；Caddy 反代默认无读超时，不会掐断 300s 生成 deadline。
