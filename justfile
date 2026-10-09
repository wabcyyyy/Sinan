# 单命令入口（G-3.5）：把散落的脚本收敛成 just dev / just test / just check。
#
# 需要 just（https://github.com/casey/just）：`uv tool install rust-just` 或
# `winget install Casey.Just`。没装 just 时 AGENTS.md 里的原命令仍然有效——
# 本文件只是入口，不是唯一路径。
#
# 设计口径：
# - check 是唯一"全量门禁"入口，直接委托 travel-agent-python/scripts/check.ps1，
#   保证本地与 CI 同序同果（不在这里重复一遍步骤，避免两处漂移）；
# - 命令体尽量薄：复杂逻辑留在 scripts/ 下的脚本里（AI 写的必须有人读懂，
#   四行的 just 配方比二十行的内联更好维护）。

set shell := ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# 后端包目录（just 的 working-directory 语法要求目录先存在）
py := "travel-agent-python"
fe := "travel-frontend-react"

# 列出全部可用命令
default:
    @just --list

# ---- 门禁 ----

# 一键门禁：ruff → format → pyright → import-linter → 规模 → 密钥 → 测试(+覆盖率) → diff-cover → 契约漂移
check:
    cd {{py}}; powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1

# 后端离线测试（不含覆盖率，快；tests/agent_eval 度量层单测在内，PR-0 收紧）
test:
    cd {{py}}; uv run pytest tests/ -q --ignore=tests/api --ignore=tests/perf

# 离线评测（行为类改动后对比指标；跑完即棘轮，指标动了红；体验题集 fail>0 自红）
eval:
    cd {{py}}; uv run python tests/agent_eval/eval_agent.py
    cd {{py}}; uv run python tests/agent_eval/eval_research.py
    cd {{py}}; uv run python tests/agent_eval/eval_editing.py
    cd {{py}}; uv run python tests/agent_eval/eval_experience.py
    cd {{py}}; uv run python scripts/eval_ratchet.py

# 流式事件快照（P2 拆分/重构的判据）
snapshot:
    cd {{py}}; uv run pytest tests/test_stream_snapshot.py -q

# 契约导出（改 app/schemas 后必须跑，产物随同 commit 入仓）
contracts:
    cd {{py}}; uv run python scripts/export_contracts.py

# 外源健康探针（LA1）：对已接外源发最小真实请求，产出死活表；**不进 just check**
# （CI 无外网是既有假设）。手工/夜间跑，跑完把结论与当日日期抄进 SOURCES.md。
# 判定口径见 scripts/probe_sources.py：401=活着缺 key，404=死了，连不上须 DoH 复核。
probe *args='':
    uv run --project {{py}} python {{py}}/scripts/probe_sources.py {{args}}; exit $LASTEXITCODE

# ---- 前端 ----

# 前端外观与依赖门禁 + 构建
fe-check:
    cd {{fe}}; npm run build
    cd {{fe}}; npm run theme:lint
    cd {{fe}}; npm run ep:lint
    cd {{fe}}; npm run test:unit

# trek 双站像素对照（v2.8 视觉复刻验收）：本地 dev server 与 demo.liketrek.com 同视口截图 +
# 并排图 + pixelmatch 差异热图，产物落 travel-frontend-react/.tmp-vdiff/。先起 just dev-fe 再跑
vdiff:
    cd {{fe}}; node scripts/vdiff.mjs

# ---- 开发 ----

# 起后端（需本机 MySQL/Redis，见 README）
dev:
    cd {{py}}; uv run python main.py

# 起前端开发服务器
dev-fe:
    cd {{fe}}; npm run dev

# 起本地基础设施（MySQL/Redis；语料库退役后不再需要 Qdrant）
dev-infra:
    docker compose up -d mysql redis

# ---- P0 backup / restore ----
# Caller MUST stop all writers first and keep MySQL running; these recipes never stop services.
# Bundles contain content/dump.sql + content/data/ (all backend files, including empty dirs).
# Output parent must exist. Restore needs an existing EMPTY DB, an ABSENT data dir,
# a trusted bundle and explicit --yes; partial SQL failures are NOT rolled back.
# Pass arguments as one quoted string when paths contain spaces, e.g.
# just backup '--out "D:/backups/travel-20260917"'
# Isolated rehearsal: use --compose-file, --project-name AND --data-dir together;
# the alternate compose file must use separate containers/volumes/ports.
# Defaults only show help; restore never supplies confirmation automatically.
backup *args='--help':
    uv run --project {{py}} python {{py}}/scripts/backup_restore.py backup {{args}}; exit $LASTEXITCODE

restore *args='--help':
    uv run --project {{py}} python {{py}}/scripts/backup_restore.py restore {{args}}; exit $LASTEXITCODE
