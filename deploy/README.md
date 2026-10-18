# 生产部署入口（自托管 / Caddy 全栈）

> 托管平台路径（Railway 单实例 + 持久卷）见 [docs/public/production.md](../docs/public/production.md)；
> 本目录是不依赖平台的**自托管备选**：Caddy 终结 TLS 的单机 Compose 栈。
> 根目录的 `docker-compose.yml` 继续是本地/演示编排，两者刻意不合并、不互相引用：
> 口径不同（下表），合并只会把预览面的宽松项带进生产。

- 命令入口：`deploy/deploy.sh`（构建→开账号→迁移→起栈→验收→回滚）
- 编排：`deploy/docker-compose.prod.yml`
- 边缘：`deploy/Caddyfile`（TLS、安全头、请求体分档、内部面公网拒绝、SSE 不缓冲）
- 数据库账号：`deploy/provision.sql`（最小权限，人跑或 deploy.sh 跑，幂等）

`app/common/config_primitives.py` 的占位符拦截（应用侧）与 `tests/test_body_size_limit.py`、
`tests/test_boot_secret_placeholders.py`（两层口径）引用的就是这里的 `Caddyfile` 与 `deploy.sh`：
部署侧 `deploy.sh` 拦一道，应用侧 `validate_boot` 再拦一道。

## 1. 与预览编排的差异（为什么不能拿 docker-compose.yml 上线）

| 维度 | 预览 `docker-compose.yml` | 生产 `deploy/docker-compose.prod.yml` |
| --- | --- | --- |
| TLS | nginx 只 listen 80（明文） | Caddy 终结 TLS，公网只有 443 + 80 跳转 |
| 会话 Cookie | `AUTH_COOKIE_SECURE: "false"` | `AUTH_COOKIE_SECURE: "true"`（env 改不动，写在 `environment:` 里） |
| 代理归因 | `TRUSTED_PROXIES` 含 `172.16.0.0/12`（整段私网） | 只填 `sinan-edge` 网段；Caddy 用 `{remote_host}` **替换** XFF |
| DB 身份 | `DB_USER: root` | 运行账号只有 SELECT/INSERT/UPDATE/DELETE；结构变更走迁移账号 |
| 应用端口 | 后端 `127.0.0.1:8000` 宿主可直连 | 后端/前端/MySQL/Redis **一个都不发布**；宿主只看到 443/80 |
| 数据库网段 | 普通 bridge | `sinan-data` 是 `internal: true`，无对外路由 |
| 停止 | `stop_grace_period: 45s`（≥ 全局 30s 排空预算） | 后端 150s（盖过 `shutdown.drain` 的全局 30s 排空，留足退出余量） |
| 资源/日志 | 无 cpu/mem 上限；日志 50m×5 | 每容器 cpu/mem 上限 + 日志 20m×5 |
| 容器身份 | 全部默认 root 起 | 除 MySQL 外全非 root；`read_only` + `tmpfs` + `cap_drop ALL` + `no-new-privileges` |
| 镜像 | `latest` 语义，无溯源 | 标签=git SHA，OCI `revision` 标签由构建校验 |
| 数据目录 | bind `./travel-agent-python/data` | 命名卷 `sinan-prod-app-data`（备份脚本的适配见 §7） |

## 2. 干净机器上的完整步骤

前置：Linux（或 Docker Desktop）+ docker CLI + Compose v2 + git + curl；`bash` 执行 `deploy.sh`。
`deploy/deploy.sh` 必须以 **LF** 检出（`deploy/.gitattributes` 已钉住），CRLF 版会让 bash 报错。

```bash
# 0) 到仓库根，确认工作树干净（脏树要显式授权，见 §5）
git rev-parse HEAD
git status --short

# 1) 建未入库的 env 目录（deploy/secrets/ 命中 .gitignore:38 的 secrets/ 规则，永不会入仓）
mkdir -p deploy/secrets

# 2) 写 env（键与含义见 §3；口令生成：openssl rand -hex 32）
$EDITOR deploy/secrets/prod.env

# 3) 只做插值校验（不启动任何东西）
SINAN_IMAGE_TAG=$(git rev-parse HEAD) \
  docker compose -f deploy/docker-compose.prod.yml --env-file deploy/secrets/prod.env config --quiet

# 4) 首次（或轮换口令时）开通数据库账号：需要 MySQL 先起来
deploy/deploy.sh provision

# 5) 发布：建镜像(打 SHA 标签) → 迁移(迁移账号) → 起栈 → 深探与边缘验收
deploy/deploy.sh deploy

# 6) 事后复验 / 看状态
deploy/deploy.sh verify
deploy/deploy.sh status
```

`deploy` 的验收不是"容器在跑"，而是三条同时成立：
① 深探 `mysql=up redis=up`；② 公网侧 `/api/agent/*`、`/mcp`、`/api/agent/v1/metrics` 全 403；
③ SPA 首页经 TLS 返回 200。任一条不满足脚本就非零退出。

## 3. env 键逐个说明

必填（缺一个 compose 就拒绝插值）：

| 键 | 用在哪 | 为什么 |
| --- | --- | --- |
| `SINAN_DOMAIN` | Caddy 站点地址、`AGENT_CORS_ORIGINS` | 公网源。**入库默认是占位符**，真域名 + DNS 是签发证书的前提 |
| `SINAN_EDGE_SUBNET` | `sinan-edge` 网段 | 固定子网，好让 XFF 信任口径写成网段而不是漂移的容器 IP |
| `SINAN_DATA_SUBNET` | `sinan-data` 网段 | DB/缓存的隔离网段（internal） |
| `SINAN_TRUSTED_PROXIES` | 后端 `TRUSTED_PROXIES` | 必须等于 `SINAN_EDGE_SUBNET`；填 `0.0.0.0/0`/`172.16.0.0/12` 会被 `deploy.sh` 直接拒 |
| `SINAN_DB_NAME` | MySQL 建库 + 两个账号的 GRANT 范围 | 单一库名口径 |
| `SINAN_DB_USER` / `SINAN_DB_PASSWORD` | 应用连库身份 | 只有 DML，没有任何 DDL |
| `SINAN_MIGRATE_USER` / `SINAN_MIGRATE_PASSWORD` | 发布窗口的迁移身份 | DML + CREATE/DROP/ALTER/INDEX/REFERENCES，只在这一步用 |
| `SINAN_DB_HOST_PATTERN` | MySQL 账号的 host 模式 | 锁到 `sinan-data` 网段（如 `172.28.204.%`）；换网段要同步换它 |
| `SINAN_MYSQL_ROOT_PASSWORD` | 只给 MySQL 初始化/healthcheck/人工运维 | 应用拿不到 root |
| `SINAN_REDIS_PASSWORD` | Redis `--requirepass` + 后端 URL 派生 | 库里是吊销黑名单与登录锁桶 |
| `SINAN_JWT_SECRET` | 会话票签名（≥32） | 占位符串（`replace-with`/`your-`/`changeme`）会被 `deploy.sh` 与 `validate_boot` 两层拒 |
| `SINAN_AGENT_INTERNAL_TOKEN` | 内部面令牌 | `AGENT_HOST` 非回环时启动期硬要求 |

可选：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `SINAN_BYOK_ENC_KEY` | 空=从 `JWT_SECRET` 派生 | 显式配第三把钥匙，轮换 JWT 才不会让存量 BYOK 密文失效 |
| `SINAN_TLS_DIRECTIVE` | 空=自动 ACME | 填 `tls internal` 走 Caddy 内置 CA（无 DNS 演练用）。生产留空 |
| `SINAN_TLS_PUBLIC_PORT` / `SINAN_HTTP_PUBLIC_PORT` | `443` / `80` | 只改**宿主侧**端口，服务隔离演练；容器内仍是 443/80 |
| `SINAN_IMAGE_REPO` / `SINAN_IMAGE_REPO_FRONTEND` | `sinan/backend`、`sinan/frontend` | 镜像名；换仓库时与 `SINAN_REGISTRY` 一起改 |
| `SINAN_REGISTRY` | 空=不推送 | 不设就跳过 push 并打印缺项（见 §6） |
| `SINAN_APP_ENV_FILE` | `secrets/app.env` | 模型/供应商等**可选**键的容器级 env_file。`environment:` 优先级更高，安全键不可能被它覆盖 |
| `SINAN_CADDY_IMAGE` | `caddy:2.10-alpine` | 边缘镜像版本；换版本要重跑演练 |
| `SINAN_UP_TIMEOUT` | `300` | `up --wait` 的秒数 |
| `SINAN_RUN_PROVISION` | `0` | `deploy` 默认不碰账号；`1` 才在发布流程里跑 provision |
| `SINAN_ENV_FILE` | `deploy/secrets/prod.env` | env 文件位置。本次隔离演练用的是仓库内 `.tmp-t8a-drill/prod.env`（命中 `.gitignore:70` 的 `.tmp-*`），用完即删 |

模型侧的键（`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_ROLE_*`、`OTM_API_KEY`、
`NOMINATIM_*`、`UA_CONTACT`、`LLM_ALERT_WEBHOOK_URL` 等）**不在这里重复登记**：
权威清单是 `travel-agent-python/.env.example` + `app/common/config.py`。
生产机上的做法是把它们写进 `deploy/secrets/app.env`（或 `deploy/.env`，与后端 `.env` 同名规则），
容器通过 `env_file` 拿到。未配 `LLM_API_KEY` 时进程能起、探活能过，但第一次生成就会失败——
这是应用侧既有行为，`deploy.sh` 会打印一行警告而不是替你猜。

## 4. 发布后自检（每条都是可执行命令）

`deploy/docker-compose.prod.yml` 的 `image:` 用了 `${SINAN_IMAGE_TAG:?}`，所以**任何**
compose 调用都要先有镜像标签——手敲命令时最容易漏这一条：

```bash
export SINAN_ENV_FILE=deploy/secrets/prod.env
export SINAN_IMAGE_TAG=$(git rev-parse HEAD)
C() { docker compose -f deploy/docker-compose.prod.yml --env-file "$SINAN_ENV_FILE" "$@"; }
D="$SINAN_DOMAIN"        # 从 env 文件里取，别手抄
TLS_PORT="${SINAN_TLS_PUBLIC_PORT:-443}"

# 4.1 只有 443/80 对宿主发布（其余容器 Ports 只能是没有宿主侧的空括号）
C ps -a
docker ps --filter label=com.docker.compose.project=sinan-prod --format '{{.Names}} | [{{.Ports}}]'

# 4.2 深探：经 Caddy 的内部探测面，不是直连后端
C exec -T caddy curl -fsS http://127.0.0.1:8081/api/agent/health/deep
# 期望 {"code":200,...,"data":{"mysql":"up","redis":"up","status":"up"}}

# 4.3 公网侧必须拒绝内部面（403），业务面按自身鉴权答（匿名 401）
for p in /api/agent/health /api/agent/health/deep /api/agent/v1/metrics /mcp; do
  printf '%s ' "$p"; curl -sk --resolve "$D:$TLS_PORT:127.0.0.1" -o /dev/null -w '%{http_code}\n' "https://$D:$TLS_PORT$p"
done
curl -sk --resolve "$D:$TLS_PORT:127.0.0.1" -o /dev/null -w '/api/user/info(匿名) %{http_code}\n' "https://$D:$TLS_PORT/api/user/info"
# 内置 CA 的演练要加 -k；真实证书就去掉 -k，让 curl 验签（deploy.sh 按 SINAN_TLS_DIRECTIVE 自动决定）

# 4.4 Secure Cookie 实测：登录响应的 Set-Cookie 必须带 Secure
curl -sk --resolve "$D:$TLS_PORT:127.0.0.1" -D - -o /dev/null -X POST \
  -H 'Content-Type: application/json' \
  -d '{"username":"<一次性账号>","password":"<6-24 位字母+数字>"}' \
  "https://$D:$TLS_PORT/api/auth/login" | grep -i '^set-cookie'
# 期望 Set-Cookie: TA_AUTH=...; HttpOnly; Max-Age=86400; Path=/; SameSite=lax; Secure

# 4.5 明文 80 只做跳转
curl -s --resolve "$D:${SINAN_HTTP_PUBLIC_PORT:-80}:127.0.0.1" -o /dev/null \
  -w 'http=%{http_code} -> %{redirect_url}\n' "http://$D:${SINAN_HTTP_PUBLIC_PORT:-80}/"

# 4.6 后端仍能出网（sinan-data 是 internal 网，默认网关只可能来自 sinan-edge）
C exec -T agent-python python -c "
import urllib.request,urllib.error
try: print('egress status', urllib.request.urlopen('https://api.deepseek.com',timeout=20).status)
except urllib.error.HTTPError as e: print('egress OK (上游 HTTP', e.code, '，TCP+TLS 出得去)')
except Exception as e: print('egress FAILED:', type(e).__name__, str(e)[:120])"

# 4.7 运行账号真的没有 DDL（用应用容器里的身份连库，口令不上命令行）
C run --rm --no-deps --no-TTY agent-python python -c "
import os,pymysql
c=pymysql.connect(host=os.environ['DB_HOST'],port=int(os.environ['DB_PORT']),user=os.environ['DB_USER'],
                  password=os.environ['DB_PASSWORD'],database=os.environ['DB_NAME'])
cur=c.cursor()
cur.execute('SELECT COUNT(*) FROM sys_user'); print('SELECT ok, rows =', cur.fetchone()[0])
try: cur.execute('CREATE TABLE t8a_ddl_probe(a int)'); print('DDL_ALLOWED (BAD)')
except Exception as e: print('DDL_DENIED_OK ->', str(e)[:90])"

# 4.8 停止宽限：真正等一次优雅停止（空闲栈应当远小于 150s，且每个容器 exit=0）
time C stop
C ps -a          # 状态必须都是 Exited (0)，出现 137 就是被 SIGKILL 了
```

空闲栈的停止检查不能替代带生成任务时的排空验证。后者会消耗真实模型额度，需在部署环境单独安排。

## 5. 脏工作树与"这台机器不是构建机"

`deploy.sh` 从 **`git archive HEAD`** 构建（`.tmp-deploy-<sha>/`，用完可删），所以：

- 镜像内容 = 那个 SHA 的已提交内容，工作树的未提交改动**进不去**；
- OCI `revision` 标签因此是可信的，`docker inspect` 能反查；
- 反过来，工作树里有别人的 dirty 时脚本默认**拒绝**执行（并发任务共享同一 checkout 是这里的
  现实），要显式 `SINAN_DEPLOY_ALLOW_DIRTY=1`，且它只放宽"检查"，不放宽"构建上下文"。

挂进 Caddy 容器的 `Caddyfile` 也来自同一份归档（`SINAN_CADDYFILE`），
即"跑起来的边缘配置 = 那个 SHA 里的边缘配置"。

## 6. 镜像推送、DNS、证书：脚本代不了的部分

`deploy.sh` 检测到缺项时**打印缺什么**并继续把能做的做完，不会伪造成功：

- `SINAN_REGISTRY` 未设 → 跳过 `docker push`。后果：发布记录只剩本机 `docker images` 里的
  SHA 标签，宿主重装即丢，`rollback` 只在本机可用。
- `SINAN_DOMAIN` 是占位符/DNS 未指向本机 → Caddy 的 ACME 签发失败（80 必须对本域名公网可达）。
  无 DNS 的演练请用 `SINAN_TLS_DIRECTIVE=tls internal`。
- 当前没有 SMTP 配置或邮件找回流程；告警出口为 `LLM_ALERT_WEBHOOK_URL`。
- 异地备份、外部拨测、依赖与镜像漏洞扫描需要在部署环境配置。

## 7. 部署验收与已知边界

部署后按第 4 节在自己的环境验证数据库权限、内部端点隔离、TLS、登录 Cookie、出网与停止行为。
真实域名的 ACME 签发、生成任务运行时的排空、SSE 经反向代理的实际流式交付，需分别验证。

- `deploy.sh` 不产出发布 manifest；需要保留发布记录时，将日志、Git revision 和镜像 digest 存入自己的运维记录或 CI artifact。
- 生产使用命名卷，`scripts/backup_restore.py` 只处理宿主数据目录；完整备份边界见下一节。
- 指标端点与 `LLM_ALERT_WEBHOOK_URL` 需要接入自己的采集与告警系统。
- CI 的 `release.yml` 不包含镜像构建、推送和部署；漏洞扫描、外部拨测与容量验收需在部署环境完成。

## 8. 轮换、回滚与备份边界

**改配置的正确姿势**（`admin off` 意味着 `caddy reload` 不可用，靠重启容器）：

```bash
deploy/deploy.sh verify                  # 改完 env 先复验
$C up -d --force-recreate caddy          # 改了 Caddyfile
$C up -d --force-recreate agent-python   # 改了应用 env
```

- **证书**：ACME 由 Caddy 自动续期，证书与内置 CA 存在命名卷 `sinan-prod-caddy-data`（`/data`）。
  删这个卷=全部重签，ACME 有速率限制。续期失败先看 `docker compose logs caddy | grep -i acme`。
- **DB 口令**：改 `deploy/secrets/prod.env` → `deploy/deploy.sh provision`（`ALTER USER` 会把
  口令与连接上限对齐）→ `up -d --force-recreate agent-python`。
- **`SINAN_MYSQL_ROOT_PASSWORD`**：env 改了不会自动改库里的 root 口令（root 由镜像初始化时创建）。
  轮换要么进容器 `ALTER USER 'root'@'localhost' IDENTIFIED BY ...`，要么重建实例并恢复备份。
  轮换时须同步实际数据库账号与环境配置。
- **`SINAN_JWT_SECRET`**：换它=所有人会话失效（预期行为）；若 BYOK 密文是从它派生的，
  用户要重录 API key。所以生产显式配 `SINAN_BYOK_ENC_KEY`。
- **`SINAN_AGENT_INTERNAL_TOKEN`**：换它之前确认没有外部脚本还在用旧值（`/mcp`、运维 curl）。

**回滚**：

```bash
deploy/deploy.sh rollback <上一个可发布的完整 40 位 SHA>
```

它只把运行镜像指回那个 SHA 的标签并重新验收（深探 + 边缘），**不动 schema**：
迁移是 append-only 且没有 down 路径。如果目标 SHA 早于当前 schema，缺列写入会以权限错误或
500 现身——那属于"从备份恢复"，见下一段，不要指望本命令。

**备份边界**：
`scripts/backup_restore.py` 现在按宿主目录 `travel-agent-python/data/` 工作，而生产用命名卷
`sinan-prod-app-data`；它也不含 Redis、env 密钥与 MySQL 账号。所以生产要能恢复，至少还需要：
命名卷的 tar（`docker run --rm -v sinan-prod-app-data:/d alpine tar`）或把这里换成 bind 目录、
`sinan-prod-caddy-data`（证书/内置 CA）、`deploy/secrets/*` 的密管来源、以及一次隔离恢复演练。
**在这些补齐之前，不要把本目录称为"可恢复的部署"。**
