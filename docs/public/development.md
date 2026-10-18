# 开发与验证

需要 Python 3.12、uv、Node 22.12+。CI 与 Docker 使用 Node 22，依赖以锁文件为准。

先按 README 初始化配置，执行 `docker compose up -d mysql redis`。
后端目录运行 `uv sync --frozen`，再运行 `uv run python main.py`。
前端目录运行 `npm ci`，再运行 `npm run dev`。完整页面样例只需前端，打开 `/demo`；`npm run build:demo` 构建免登录静态展示（入口 `/`），部署见 [Vercel 展示说明](vercel-demo.md)。
宿主机连接基础设施时检查 DB_PORT/REDIS_PORT 与根环境文件一致。

## 后端

日常在根目录运行 `verify.ps1`（或 `just check`），规则见 `VALIDATION.md`。
完整门禁交给 CI；用户明确要求本地全量时，在 `travel-agent-python/` 执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1 -Full
```

包含 Ruff、类型、分层、规模、秘密扫描、离线测试、变更行覆盖率与契约漂移，CI 在 Linux 执行对应步骤。
相关行为可按范围点名 smoke，以下评测全集由 CI 执行：

```bash
uv run python tests/agent_eval/eval_agent.py
uv run python tests/agent_eval/eval_research.py
uv run python tests/agent_eval/eval_editing.py
uv run python tests/agent_eval/eval_experience.py
uv run python scripts/eval_ratchet.py
```

`eval_agent.py` 默认走业务层产品路径（`plan_days` 在临时 SQLite 上真跑，按落库读回的交付结果计分）；`--path graph` 跑 Agent 面的 trip 分支作对照，报告另存 `report_graph.*`，不进棘轮。评测期间 LLM HTTP 出口有哨兵，任何真实调用都会让该用例报错。

schema 变更执行 `uv run python scripts/export_contracts.py`，生成产物随同批提交。迁移只追加，门禁不放宽。
真实 API 测试、nightly 和生成压测可能调用模型，不属于默认离线门禁。

题集、校准数据、golden 快照与 `tests/agent_eval/metrics_baseline.json` 入仓；
`tests/agent_eval/report/`、`tests/experience_cases/report/` 和 `tests/perf/report/` 是本地生成输出，不入仓。
全新 clone 运行上面的评测命令后再运行 `scripts/eval_ratchet.py`，报告缺失仍会报错，不能用历史输出替代本次评测。
CI 将运行报告保存为 artifact。个人计划、AI 交接、审查与原始验收包也只在本地保留；公开文档放在 `docs/public/`。

业务生成任务从提交起共享 `ITINERARY_DEADLINE_SECONDS`（默认 300 秒），研究段另受
`RESEARCH_DEADLINE_SECONDS`（默认 60 秒）约束。整段生成、逐日修复和富化共用调用、检索和
token 预算；排队也计时。同一用户同时只能有一个规划任务，持久化预算与 checkpoint 共用
SQLite，恢复时延续已消耗额度。单独 Agent 图运行仍用 `AGENT_DEADLINE_SECONDS`（默认 240 秒）。

外部事实由数据适配器签发字段快照，`factEvidence.sourceRef` 引用该次取回的证据 ID。
名称、来源及字段必须与快照相符，模型补的坐标、费用和时间不能仅凭同名地点继承背书。
候选资料只在提示词显示时清洗，原始事实保持不变；价格调整后的费用重新标为估算。

## 前端

在 `travel-frontend-react/` 执行：

```bash
npm run build
npm run test:unit
npm run theme:lint
npm run ep:lint
npm run e2e:demo
```

浏览器测试需要前端运行与 Edge/Chrome，默认 Edge，可设 E2E_CHANNEL=chrome。
CI 安装 Playwright Chromium 并设 E2E_CHANNEL=bundled。展示测试阻断业务 API 与外部资源，不调用模型，验证正式页面、生成脚本、编辑确认和地图降级。测试展示构建时设置 E2E_DEMO_PREFIX 为空。
`e2e:golden` 是真实生成路径，不要与固定回放混淆。

## 生成证据采集与回放探针

复用业务探针、LangGraph 检查点和 Playwright。先启动**仅前端**，执行离线采集回放：

```powershell
cd travel-frontend-react
$env:E2E_BASE_URL = 'http://127.0.0.1:5173'
node e2e/preview-evidence.mjs
cd ../travel-agent-python
uv run python scripts/live_perf_probe.py --evidence-input tests/agent_eval/report/live/collection-smoke-实际时间戳.json
```

默认模式拦截所有业务 API 并阻断页面外网；三场景回放含成功、无关日期改动/新增违规、
失败与候选撤回，只验证采集完整性，不能引用回放提前量或修复率作为真实模型成果。
规则侧单测还覆盖 unknown、必去丢失、检查点缺失、同题重复与时间格式归一。
Windows 若临时目录权限异常，将 TEMP/TMP、UV_CACHE_DIR 指向仓库内可写目录。

真实调用会消耗模型额度并写入数据库：使用同一命令追加 `--live`，后端须已用当前 checkout/config 启动，
`E2E_REPEATS=1` 对 `tests/agent_eval/live_pilot_cases.json` 三题各跑一次；clarify 回放冻结输入，
生成/研究/修复/落库均真实。浏览器采集与业务生成是**同一批请求**。首次先查三题采集完整性，
不自动扩到 24 次；失败、unknown、新增违规不剔除，也不为补齐漂亮结果自动重跑。
默认单请求等待上限 300 秒；这是采集超时，**不是服务端取消或费用上限**。
无法确认终态时保留该次记录并自动停批，确认后台状态后再决定是否开启新的批次。

原始及 `.analyzed.json` 都在既有 `report/live/`；文件名默认唯一，分析输出禁止覆盖。
初始基线 = 首个整趟 checkpoint（确定性落地之后、业务逐日重生成之前），不是原始模型文本。
报告保存首次计划、逐日重试检查点、最后一次数据库详情读回及全部 SSE/详情历史。
缺首次检查点或终态详情会显式记缺口；没有完整初稿的失败不能算作已修复。
约束复用 check_constraints 与 validate_plans；违规单元是独立类型约束及每日日规则合取项，
不等同于独立错误条数。unknown 不当 pass，全程必去与预算按整趟检查；费用只验估算。
未涉及日期指没有逐日重生成记录的日期，比较点位/顺序/时间/费用，不比较 DB id 和富化文案。

浏览器起点为最终确认点击。命名候选必须实际可见并跨过两次动画帧；骨架/「候选地点」不计。
主行程时间必须同时满足权威详情全天 SUCCEEDED、coreReady/COMPLETED 和内容已在 DOM 展示。
逐请求相减计算提前量；缺任一端保留 null，一日场景的逐项候选记不适用。这里只证明提前展示，
不表示整趟生成速度提升。服务端事件时间与浏览器展示时间分别保存，不能互换。

每次尝试保存前即落报告；代码/config/采集器 hash 与题集 hash 随报告绑定，运行期间变化会标错，
下一次尝试前检测到变化即停止该批。源代码、模型/Prompt、预算/开关、题面、缓存协议或浏览器
环境变化须分批；采集口径变化后可从完整原始记录重算，但不能补造缺失的展示时刻。
单纯报告排版变化不改变原始请求；仅修改分析代码时，旧记录是否可重算由快照/事件是否齐全决定。
旧 live_perf_report 的 SSE 起点时间与新版 POST 起点绝对时间不可合并；修复前后也不使用两次独立生成作配对。
检查点以本次尝试的起止时间限定，避免把以后手动重生成混入同请求。城市字典/消费基准须在运行前固定；
源码中的迁移 SQL hash 不证明数据库已应用版本，不能把本地源码/config 身份冒充运行中服务的身份证明。

## 字体维护

从 provenance.json 指定的官方提交下载原始字体，在后端运行 `uv run python scripts/build_preview_font.py SOURCE.ttf`。
脚本验证原始 SHA256，输出静态 TTF 与台账。fontTools 仅为开发依赖；运行镜像使用已提交字体与 OFL。

Docker 构建通过 Compose 提供只含根 LICENSE/第三方说明的 named context，保证镜像保留许可。
单独构建 Dockerfile 时也要传入 `--build-context project=..`（在对应应用目录执行）。
