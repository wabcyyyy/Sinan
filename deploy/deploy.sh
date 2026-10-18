#!/usr/bin/env bash
# ===================== 司南生产发布入口（T8a）=====================
# 用法：
#   deploy/deploy.sh deploy                 # 建镜像(SHA) → 起基础设施 → 开账号(需显式) → 迁移 → 起全栈 → 验收
#   deploy/deploy.sh provision              # 只跑 deploy/provision.sql（幂等，人工运维走同一条路径）
#   deploy/deploy.sh migrate                # 只用迁移账号跑应用自己的 ensure_schema
#   deploy/deploy.sh verify                 # 复跑验收（深探 + 公网侧拒绝内部面 + 端口清单）
#   deploy/deploy.sh rollback <full-sha>    # 用该 SHA 的既有镜像重新起栈（不动 schema）
#   deploy/deploy.sh status | down
# 缺 env 文件、脏树、占位符密钥、权限不够都会当场拒绝，不会"看起来成功了"。
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
readonly SCRIPT_DIR REPO_ROOT
cd "${REPO_ROOT}"

readonly COMPOSE_FILE="deploy/docker-compose.prod.yml"
readonly PROVISION_SQL="deploy/provision.sql"
readonly ENV_FILE="${SINAN_ENV_FILE:-deploy/secrets/prod.env}"
readonly UP_TIMEOUT="${SINAN_UP_TIMEOUT:-300}"
readonly INTERNAL_PROBE="http://127.0.0.1:8081"
# 镜像仓库名在 load_env_vars 之后才定（SINAN_IMAGE_REPO 可能只写在 env 文件里）。
BACKEND_IMAGE_REPO=""
FRONTEND_IMAGE_REPO=""
# 与 travel-agent-python/app/common/config.py:48 的 _SECRET_PLACEHOLDER_PREFIXES 同口径
# （两层防线：deploy.sh 在部署侧拦，validate_boot 在应用侧拦）。改一边必须同步另一边。
readonly SECRET_PLACEHOLDER_PREFIXES=("replace-with" "your-" "changeme")

GIT_SHA=""
TMP_DIR=""

log() { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy][WARN] %s\n' "$*" >&2; }
die() { printf '[deploy][FAIL] %s\n' "$*" >&2; exit 1; }

cleanup() {
	# 渲染出来的 provision.sql 含口令：无论成功失败都要抹掉。
	if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
		rm -rf "${TMP_DIR}"
	fi
}
trap cleanup EXIT

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

compose() { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }

# ---------- 前置闸门 ----------

need_cmds() {
	local c
	for c in git docker curl; do
		command -v "${c}" >/dev/null 2>&1 || die "缺少命令：${c}"
	done
	docker compose version >/dev/null 2>&1 || die "docker compose 不可用（需要 Compose v2）"
}

# 占位符密钥拦截：公开仓库里的示例值能穿过长度校验（config.py:42-47 记录了这件事），
# 所以部署侧先拒一道。空串不在这里判：必填键由 compose 的 ${VAR:?} 拒。
reject_secret() {
	local name="$1" value="$2" min_len="${3:-0}" prefix lower
	lower=$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')
	for prefix in "${SECRET_PLACEHOLDER_PREFIXES[@]}"; do
		case "${lower}" in
		"${prefix}"*)
			die "${name} 仍是占位符串（${prefix}…，来自 .env.example 的公开示例值）：用 openssl rand -hex 32 生成真实随机串"
			;;
		esac
	done
	if [ "${#value}" -lt "${min_len}" ]; then
		die "${name} 长度 ${#value} < ${min_len}：弱密钥可被离线伪造会话票"
	fi
}

# provision.sql 把每个值都包在引号或反引号里，所以这里只卡"能提前结束字面量"的字符。
# 允许 - ; 空格这类在带引号的字面量里无害的字符，否则口令字符集会被无谓地收窄。
assert_sql_safe() {
	local name="$1" value="$2"
	case "${value}" in
	*"'"* | *'"'* | *'\'* | *'`'* | *'${'* | *'}'*)
		die "${name} 含引号/反斜杠/反引号/花括号之一，会破坏 provision.sql 的字面量，请换值"
		;;
	esac
	[ -n "${value}" ] || die "${name} 为空"
}

require_env_file() {
	[ -f "${ENV_FILE}" ] || die "缺少 env 文件：${ENV_FILE}（未入库。键清单见 deploy/README.md；deploy/secrets/ 命中 .gitignore:38 的 secrets/ 规则）"
	[ -r "${ENV_FILE}" ] || die "env 文件不可读：${ENV_FILE}"
	log "env 文件：${ENV_FILE}（本脚本不打印它的值）"
}

# 只把要检查的键读进 bash：不 source，避免 env 文件里的任意 shell 语法被执行。
# shellcheck disable=SC2163
load_env_vars() {
	local line key value
	while IFS= read -r line || [ -n "${line}" ]; do
		line="${line%$'\r'}"
		case "${line}" in
		'' | '#'*) continue ;;
		esac
		key="${line%%=*}"
		value="${line#*=}"
		key="${key//[[:space:]]/}"
		value="${value#"${value%%[![:space:]]*}"}"
		value="${value%"${value##*[![:space:]]}"}"
		value="${value%\"}"
		value="${value#\"}"
		case "${key}" in
		SINAN_*) export "${key}=${value}" ;;
		esac
	done <"${ENV_FILE}"
}

preflight() {
	need_cmds
	require_env_file
	load_env_vars

	reject_secret SINAN_JWT_SECRET "${SINAN_JWT_SECRET:-}" 32
	reject_secret SINAN_AGENT_INTERNAL_TOKEN "${SINAN_AGENT_INTERNAL_TOKEN:-}" 32
	reject_secret SINAN_MYSQL_ROOT_PASSWORD "${SINAN_MYSQL_ROOT_PASSWORD:-}" 12
	reject_secret SINAN_REDIS_PASSWORD "${SINAN_REDIS_PASSWORD:-}" 12
	if [ -n "${SINAN_BYOK_ENC_KEY:-}" ]; then
		reject_secret SINAN_BYOK_ENC_KEY "${SINAN_BYOK_ENC_KEY}"
	else
		warn "SINAN_BYOK_ENC_KEY 未配：BYOK 密文从 JWT_SECRET 派生，轮换 JWT_SECRET 会让存量密文失效"
	fi

	local k
	for k in SINAN_DB_NAME SINAN_DB_USER SINAN_DB_PASSWORD SINAN_MIGRATE_USER SINAN_MIGRATE_PASSWORD
	do
		[ -n "${!k:-}" ] || die "缺少 ${k}（provision.sql 要用）"
		assert_sql_safe "${k}" "${!k}"
	done
	# 账号名/库名/主机模式都会被包进引号或反引号，先卡住能越出字面量的字符。
	assert_sql_safe SINAN_DB_HOST_PATTERN "${SINAN_DB_HOST_PATTERN:-}"
	for k in SINAN_DOMAIN SINAN_EDGE_SUBNET SINAN_DATA_SUBNET SINAN_TRUSTED_PROXIES SINAN_DB_HOST_PATTERN; do
		[ -n "${!k:-}" ] || die "缺少 ${k}"
	done

	# XFF 归因闸门：TRUSTED_PROXIES 里出现"全世界"就等于把按 IP 的限速交给请求者
	# （app/common/client_ip.py 只信命中网段的对端送来的链）。
	case "${SINAN_TRUSTED_PROXIES}" in
	*0.0.0.0/0* | *172.16.0.0/12* | *10.0.0.0/8* | *192.168.0.0/16*)
		die "SINAN_TRUSTED_PROXIES=${SINAN_TRUSTED_PROXIES} 过宽：应等于 sinan-edge 网段"
		;;
	esac
	if [ "${SINAN_TRUSTED_PROXIES}" != "${SINAN_EDGE_SUBNET}" ]; then
		warn "SINAN_TRUSTED_PROXIES(${SINAN_TRUSTED_PROXIES}) ≠ SINAN_EDGE_SUBNET(${SINAN_EDGE_SUBNET})：Caddy 是唯一代理时两者应相同"
	fi
	log "前置通过：domain=${SINAN_DOMAIN} edge=${SINAN_EDGE_SUBNET} data=${SINAN_DATA_SUBNET} db=${SINAN_DB_NAME} app_user=${SINAN_DB_USER} migrate_user=${SINAN_MIGRATE_USER}"
}

# compose 文件里 image 用了 ${SINAN_IMAGE_TAG:?}，所以插值校验必须在标签定下来之后跑；
# 这一步同时也是"env 里少给了哪个必填键"的权威检查（单一真源是 compose 文件本身）。
check_interpolation() {
	compose config --quiet || die "compose 插值未通过（少 \${VAR:?} 必填键就是这个形状）"
}

# ---------- git 与构建上下文 ----------

git_head_sha() { git rev-parse HEAD 2>/dev/null || die "不在 git 仓库里"; }

dirty_tree_guard() {
	# 镜像标签要能对上"这个 SHA 的内容"，而工作树里未提交的东西既不进镜像也没被 CI 看过。
	local untracked
	untracked=$(git ls-files --others --exclude-standard)
	if ! git diff --quiet HEAD || [ -n "${untracked}" ]; then
		warn "工作树相对 HEAD 有未提交改动或未跟踪文件（并发任务的 dirty 会被原样保留，本脚本不碰它们）："
		git status --short --untracked-files=normal | sed -n '1,20p' >&2
		warn "本脚本从 **HEAD 的 git archive** 构建，上述内容不会进镜像；但也不能算被本次发布验证过。"
		[ -n "${SINAN_DEPLOY_ALLOW_DIRTY:-}" ] || die "已拒绝：脏树发布需显式授权（SINAN_DEPLOY_ALLOW_DIRTY=1）"
		warn "已授权脏树：镜像 revision 标签仍是 HEAD（${GIT_SHA}），因为构建上下文就是 HEAD 的归档。"
	fi
}

# 从 HEAD 归档构建：revision 标签与镜像内容同源，且不受工作树影响。
# 路径一律写成**相对 compose 文件目录**的形式——Git-Bash/Windows 下绝对 Unix 路径
# 会让 buildkit 认不出上下文，相对路径由 compose 自己解析成宿主真实路径。
prepare_archive_context() {
	ARCHIVE_DIR_REL=".tmp-deploy-${GIT_SHA}"
	ARCHIVE_DIR="${REPO_ROOT}/${ARCHIVE_DIR_REL}"
	if [ ! -d "${ARCHIVE_DIR}/travel-agent-python" ]; then
		mkdir -p "${ARCHIVE_DIR}"
		log "导出 HEAD 归档：${ARCHIVE_DIR_REL}/（git archive ${GIT_SHA}）"
		git archive --format=tar "${GIT_SHA}" | tar -x -C "${ARCHIVE_DIR}"
	else
		log "复用已存在的 HEAD 归档：${ARCHIVE_DIR_REL}/"
	fi
	[ -f "${ARCHIVE_DIR}/deploy/Caddyfile" ] || die "归档里没有 deploy/Caddyfile：它必须已提交到 HEAD 才能被追溯构建"
	CTX_ROOT_REL="../${ARCHIVE_DIR_REL}"
	export SINAN_ROOT_CTX="${CTX_ROOT_REL}"
	export SINAN_BACKEND_CTX="${CTX_ROOT_REL}/travel-agent-python"
	export SINAN_FRONTEND_CTX="${CTX_ROOT_REL}/travel-frontend-react"
	# 挂进容器的 Caddyfile 也取自同一份归档：跑起来的边缘配置就是那个 SHA 里的边缘配置。
	export SINAN_CADDYFILE="${CTX_ROOT_REL}/deploy/Caddyfile"
}

export_build_identity() {
	local origin_url
	origin_url=$(git config --get remote.origin.url 2>/dev/null || true)
	# 仓库名此时才可读（env 文件已 load 进 shell 环境，且 shell 优先于 --env-file）。
	BACKEND_IMAGE_REPO="${SINAN_IMAGE_REPO:-sinan/backend}"
	FRONTEND_IMAGE_REPO="${SINAN_IMAGE_REPO_FRONTEND:-sinan/frontend}"
	export SINAN_IMAGE_REPO="${BACKEND_IMAGE_REPO}"
	export SINAN_IMAGE_REPO_FRONTEND="${FRONTEND_IMAGE_REPO}"
	export SINAN_IMAGE_TAG="${GIT_SHA}"
	export SINAN_SOURCE_URL="${origin_url:-local-only-unknown-origin}"
	export SINAN_BUILD_CREATED_AT="${SINAN_BUILD_CREATED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
	log "镜像标签：${BACKEND_IMAGE_REPO}:${GIT_SHA} 与 ${FRONTEND_IMAGE_REPO}:${GIT_SHA}"
}

# ---------- 构建 ----------

build_images() {
	log "构建镜像（上下文=HEAD 归档）"
	compose build agent-python frontend-react

	local img label
	for img in "${BACKEND_IMAGE_REPO}:${GIT_SHA}" "${FRONTEND_IMAGE_REPO}:${GIT_SHA}"; do
		docker image inspect "${img}" >/dev/null 2>&1 || die "镜像不存在：${img}"
		label=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "${img}")
		[ "${label}" = "${GIT_SHA}" ] || die "镜像 ${img} 的 OCI revision 标签是 '${label}'，不是 ${GIT_SHA}"
		log "溯源标签确认：${img} → revision=${label}"
	done

	# 入库（且真正挂进容器）的那份 Caddyfile 用同版本 Caddy 校验一次。
	# 用 stdin 喂文件而不是 --volume：Git-Bash 会把 / 开头的参数改写成 Windows 路径，
	# 裸挂载在跨平台上不可靠；容器里不落 /data 也不需要它。
	local caddy_img caddyfile_target out
	caddy_img="${SINAN_CADDY_IMAGE:-caddy:2.10-alpine}"
	caddyfile_target="${ARCHIVE_DIR:-${REPO_ROOT}}/deploy/Caddyfile"
	[ -f "${caddyfile_target}" ] || caddyfile_target="${REPO_ROOT}/deploy/Caddyfile"
	if out=$(docker run --rm -i \
		-e SINAN_DOMAIN="${SINAN_DOMAIN}" \
		-e SINAN_TLS_DIRECTIVE="${SINAN_TLS_DIRECTIVE:-}" \
		--entrypoint sh "${caddy_img}" \
		-c 'cat >/tmp/Caddyfile && caddy validate --adapter caddyfile --config /tmp/Caddyfile' \
		<"${caddyfile_target}" 2>&1); then
		log "Caddyfile 校验通过：${caddyfile_target#"${REPO_ROOT}/"}（${caddy_img}）"
	else
		printf '%s\n' "${out}" >&2
		die "Caddyfile 校验未通过（上面是 Caddy 自己的报错），别把坏配置塞进生产"
	fi
}

push_or_skip() {
	if [ -n "${SINAN_REGISTRY:-}" ]; then
		log "推送镜像到 ${SINAN_REGISTRY}"
		docker tag "${BACKEND_IMAGE_REPO}:${GIT_SHA}" "${SINAN_REGISTRY}/backend:${GIT_SHA}"
		docker tag "${FRONTEND_IMAGE_REPO}:${GIT_SHA}" "${SINAN_REGISTRY}/frontend:${GIT_SHA}"
		docker push "${SINAN_REGISTRY}/backend:${GIT_SHA}"
		docker push "${SINAN_REGISTRY}/frontend:${GIT_SHA}"
	else
		warn "跳过 docker push：未设 SINAN_REGISTRY（缺项=镜像仓库地址与凭据）。"
		warn "后果：本机 docker image ls 里的 SHA 标签是唯一的发布记录，宿主重装即丢，回滚只在本机可用。"
		warn "补法：docker login <registry>，然后 SINAN_REGISTRY=<registry>/sinan deploy/deploy.sh deploy"
	fi
}

# ---------- 数据库：账号开通与迁移 ----------

# 用 bash 自己展开 ${VAR}，不依赖容器里有 envsubst（mysql:8.0 镜像里没有 gettext）。
render_provision_sql() {
	local out="$1" line key value guard
	: >"${out}"
	while IFS= read -r line || [ -n "${line}" ]; do
		line="${line%$'\r'}"
		guard=0
		while [[ "${line}" =~ \$\{([A-Z_][A-Z0-9_]*)\} ]]; do
			key="${BASH_REMATCH[1]}"
			value="${!key:-}"
			[ -n "${value}" ] || die "provision.sql 需要 \$${key}，env 文件里没有这个键（或值为空）"
			line="${line//\$\{${key}\}/${value}}"
			guard=$((guard + 1))
			[ "${guard}" -le 8 ] || die "provision.sql 某行占位符替换超过 8 次，疑似递归，停"
		done
		printf '%s\n' "${line}" >>"${out}"
	done <"${PROVISION_SQL}"
}

run_provision() {
	TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sinan-provision.XXXXXX")
	chmod 700 "${TMP_DIR}" 2>/dev/null || true
	local rendered="${TMP_DIR}/provision.rendered.sql"
	render_provision_sql "${rendered}"
	chmod 600 "${rendered}" 2>/dev/null || true
	log "开通数据库账号（幂等：CREATE IF NOT EXISTS → ALTER 对齐口令与连接上限 → REVOKE ALL → 重新 GRANT）"
	# -T 关 TTY 才能把 stdin 喂进容器里的 mysql；$MYSQL_ROOT_PASSWORD 只在容器内展开。
	compose exec -T mysql sh -c 'mysql --no-defaults -uroot -p"$MYSQL_ROOT_PASSWORD" --table' <"${rendered}" ||
		die "provision.sql 执行失败（MySQL 未就绪时先 compose up -d --wait mysql）"
	rm -f "${rendered}"
	rmdir "${TMP_DIR}" 2>/dev/null || true
	TMP_DIR=""
	log "账号开通完成：口令不落盘、不打印；上面的 GRANTS 输出就是权限现状"
}

run_migrate() {
	[ -n "${BACKEND_IMAGE_REPO}" ] || die "内部错误：先跑 export_build_identity（迁移需要确定的镜像标签，不能退回工作树构建）"
	docker image inspect "${BACKEND_IMAGE_REPO}:${GIT_SHA}" >/dev/null 2>&1 ||
		die "本机没有 ${BACKEND_IMAGE_REPO}:${GIT_SHA}。先 build（deploy.sh deploy），别让 run 隐式地从工作树造一个来路不明的镜像"
	log "以迁移账号跑应用自己的迁移入口（app/db/migrate.py:ensure_schema，即 main.py:252 那一步）"
	# 本仓没有独立的迁移 CLI，应用的迁移入口就是这个函数；这里用同一个镜像、同一段代码，
	# 只把 DB_USER/DB_PASSWORD 换成迁移账号——运行账号从头到尾没有 DDL 权限。
	# `docker compose run` 没有 --no-build；这里靠上面的镜像存在性检查保证不会
	# 隐式地从工作树造一个来路不明的镜像（compose 只在镜像缺失时才 build）。
	compose run --rm --no-deps --no-TTY \
		-e DB_USER="${SINAN_MIGRATE_USER}" \
		-e DB_PASSWORD="${SINAN_MIGRATE_PASSWORD}" \
		agent-python python -c \
		"from app.db.migrate import ensure_schema; print('[migrate] ensure_schema ->', ensure_schema())" ||
		die "迁移失败（迁移账号权限不足或库不可达）。应用不会带着空表启动"
	log "迁移完成"
}

# ---------- 起栈与验收 ----------

bring_up_infra() {
	log "起基础设施并等 healthy：mysql redis"
	compose up -d --wait --wait-timeout "${UP_TIMEOUT}" mysql redis || die "mysql/redis 未达 healthy"
}

bring_up_all() {
	log "起全栈并等全部 healthy（超时 ${UP_TIMEOUT}s）"
	compose up -d --wait --wait-timeout "${UP_TIMEOUT}" || die "起栈未达全部 healthy（caddy 的 healthcheck 是深探，依赖挂掉就在这里失败）"
}

deep_health_check() {
	local payload
	payload=$(compose exec -T caddy curl -fsS --max-time 20 "${INTERNAL_PROBE}/api/agent/health/deep") ||
		die "深探不可达：Caddy 内部探测面 → 后端 → MySQL/Redis 这一段断了"
	printf '%s\n' "${payload}"
	printf '%s' "${payload}" | grep -q '"status":"up"' || die "深探结果不是 up（degraded 不算发布成功）"
	printf '%s' "${payload}" | grep -q '"mysql":"up"' || die "深探里 mysql 不是 up"
	printf '%s' "${payload}" | grep -q '"redis":"up"' || die "深探里 redis 不是 up"
	log "深探通过：mysql=up redis=up（经 Caddy 内部探测面，非直连后端）"
}

edge_check() {
	local port tls_flag base code path
	port="${SINAN_TLS_PUBLIC_PORT:-443}"
	tls_flag="-k"
	if [ -z "${SINAN_TLS_DIRECTIVE:-}" ]; then
		tls_flag=""
	else
		warn "当前是内置 CA（SINAN_TLS_DIRECTIVE=tls internal），下面的公网检查跳过了证书校验"
	fi
	base="https://${SINAN_DOMAIN}:${port}"
	for path in /api/agent/health /mcp /api/agent/v1/metrics; do
		# shellcheck disable=SC2086
		code=$(curl -s ${tls_flag} --resolve "${SINAN_DOMAIN}:${port}:127.0.0.1" -o /dev/null -w '%{http_code}' --max-time 20 "${base}${path}") ||
			die "公网侧请求失败：${base}${path}"
		[ "${code}" = "403" ] || die "内部面 ${path} 从公网可达（HTTP ${code}，期望 403）"
	done
	# shellcheck disable=SC2086
	code=$(curl -s ${tls_flag} --resolve "${SINAN_DOMAIN}:${port}:127.0.0.1" -o /dev/null -w '%{http_code}' --max-time 20 "${base}/")
	[ "${code}" = "200" ] || die "SPA 首页经 TLS 未返回 200（HTTP ${code}）"
	log "边缘检查通过：公网侧 /api/agent/*、/mcp、/api/agent/v1/metrics 均 403；/=200"
}

published_ports() {
	log "宿主实际发布的端口（生产应只有 80/443；演练按 env 换过宿主侧）："
	docker ps --filter "label=com.docker.compose.project=sinan-prod" \
		--format '  {{.Names}} | {{.Ports}}'
}

operator_gaps() {
	cat <<'EOF'

[deploy] 这些不是脚本能替你做的（缺一项就少一项上线证据）：
  1) 域名与 DNS：SINAN_DOMAIN 必须是已解析到本机的真实域名，否则 Caddy 的 ACME
     签发一定失败（80 要对本域名公网可达）。入库值是占位符。
  2) 镜像仓库：设 SINAN_REGISTRY 并 docker login；否则发布记录只在本机。
  3) SMTP / 告警目标：本仓没有任何 SMTP 配置键，唯一告警出口是 LLM_ALERT_WEBHOOK_URL
     （travel-agent-python/app/common/config.py），需要自行配置接收目标。
  4) 异地备份与恢复演练：scripts/backup_restore.py 按宿主 data/ 目录工作，而生产用
     命名卷 sinan-prod-app-data，需要单独备份该卷并验证恢复。
  5) 外部拨测、容量上限、依赖漏洞扫描：需在部署环境单独验证。
EOF
}

# ---------- 命令 ----------

cmd_deploy() {
	GIT_SHA=$(git_head_sha)
	dirty_tree_guard
	preflight
	export_build_identity
	check_interpolation
	prepare_archive_context
	build_images
	push_or_skip
	bring_up_infra
	if [ "${SINAN_RUN_PROVISION:-0}" = "1" ]; then
		run_provision
	else
		warn "未跑 provision（需显式 SINAN_RUN_PROVISION=1）。首次部署必须先：deploy/deploy.sh provision"
	fi
	run_migrate
	bring_up_all
	deep_health_check
	edge_check
	published_ports
	operator_gaps
	log "发布完成：${GIT_SHA}（${BACKEND_IMAGE_REPO}:${GIT_SHA} / ${FRONTEND_IMAGE_REPO}:${GIT_SHA}）"
}

cmd_provision() {
	preflight
	GIT_SHA=$(git_head_sha)
	export_build_identity
	check_interpolation
	bring_up_infra
	run_provision
}

cmd_migrate() {
	preflight
	GIT_SHA=$(git_head_sha)
	export_build_identity
	check_interpolation
	run_migrate
}

cmd_verify() {
	preflight
	GIT_SHA=$(git_head_sha)
	export_build_identity
	check_interpolation
	deep_health_check
	edge_check
	published_ports
}

cmd_rollback() {
	local target="${1:-}"
	[ -n "${target}" ] || die "用法：deploy/deploy.sh rollback <完整 40 位 SHA>"
	[[ "${target}" =~ ^[0-9a-f]{40}$ ]] || die "rollback 只认完整 git SHA（得到 '${target}'）：不接受 latest/分支名"
	preflight
	GIT_SHA="${target}"
	export_build_identity
	check_interpolation
	docker image inspect "${BACKEND_IMAGE_REPO}:${target}" >/dev/null 2>&1 ||
		die "本机没有 ${BACKEND_IMAGE_REPO}:${target}。回滚依赖发布时打的 SHA 标签镜像；没有就先把那次发布的镜像取回来（见 SINAN_REGISTRY）"
	docker image inspect "${FRONTEND_IMAGE_REPO}:${target}" >/dev/null 2>&1 ||
		die "本机没有 ${FRONTEND_IMAGE_REPO}:${target}"
	warn "回滚只换应用镜像标签，**不动 schema**：迁移 append-only 且没有 down 路径。"
	warn "若目标 SHA 早于当前 schema，缺列写入会以运行账号权限错误或 500 现身——那属于备份恢复，不是本命令。"
	bring_up_all
	deep_health_check
	edge_check
	log "回滚完成：运行镜像已指回 ${target}"
	operator_gaps
}

cmd_status() {
	preflight
	GIT_SHA=$(git_head_sha)
	export_build_identity
	check_interpolation
	compose ps
	published_ports
}

cmd_down() {
	[ "${1:-}" = "--force-i-understand-data-loss" ] ||
		die "本脚本不代为删除生产数据卷。确认要删就自己执行：docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE} down -v"
	preflight
	warn "按 --force-i-understand-data-loss 执行删卷（含 mysql/redis/app-data/caddy-data）。"
	compose down -v --remove-orphans
}

main() {
	local cmd="${1:-deploy}"
	if [ "$#" -gt 0 ]; then
		shift
	fi
	case "${cmd}" in
	deploy) cmd_deploy "$@" ;;
	provision) cmd_provision "$@" ;;
	migrate) cmd_migrate "$@" ;;
	verify) cmd_verify "$@" ;;
	rollback) cmd_rollback "$@" ;;
	status) cmd_status "$@" ;;
	down) cmd_down "$@" ;;
	-h | --help | help) usage ;;
	*)
		usage
		die "未知子命令：${cmd}"
		;;
	esac
}

main "$@"
