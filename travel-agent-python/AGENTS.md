# AGENTS.md — travel-agent-python

单 FastAPI 进程：业务面（`app/api/business/` + `app/services/`）与 Agent 面（`app/agent/`）同进程。先读根 `../AGENTS.md`。

## 硬不变量（INV-1 ~ INV-10）

- **INV-1** 门禁只升不降；任何豁免必须留注释与期限。
- **INV-2** 行为类重构不得改变输出：以快照测试与 eval 指标为判据，下降即 revert。
- **INV-3** 数据库迁移 append-only，禁止修改/重排既有迁移文件（SQL 真相源在 `app/db/migrations/sql/`）。
- **INV-4** 图节点不得抛异常，一律路由到 fallback 节点。
- **INV-5** SSE 事件信封固定 5 键 camelCase `{type, itineraryId, seq, ts, data}`，seq 单调。
- **INV-6** 新增 env 键必须同步 `.env.example` 与 `tests/test_env_example_alignment.py`。
- **INV-7** 环境变量只准在 `app/common/config.py` 读取，业务代码禁裸读 `os.environ`。
- **INV-8** 后台任务禁裸 `asyncio.create_task`/`threading.Thread`，必须走 `app/common/task_pool.py`。
- **INV-9** 工具默认只读、无副作用；任何外部网络调用必须带超时与响应上限（G-3.2：走 `ExternalClient` 缓存+节流+重试+熔断通道；重试只上幂等 GET，新外部源直接生在这套纪律里）。
- **INV-10** 新增依赖必须在 commit message 中给出理由。

## 参考实现（新代码先抄谁）

- **业务面**（路由 + 服务 + 信封）：抄 `app/services/itinerary_query.py` 配合 `app/api/business/` 的路由写法——瘦路由、逻辑进 service、返回走 Result 信封。
- **Agent 面**（研究编排 + 工具 + 降级）：抄 `app/agent/research/`——子任务并行、证据包汇聚、失败响亮降级、全程 trace 事件。
- **生成链路**（行程内容组装）：先读 `app/agent/README.md` 的模块蓝图——它定义每模块职责、依赖方向（`stream → workflow → generators → generation_core`，禁反向）与"新增生成能力先抄谁"。

## 双轨错误边界（何时用哪个）

- **Agent 面**：LLM/检索/解析失败是**预期路径**。函数抛 `ValueError`（或返回降级标记），由图/流式层捕获后路由到 fallback（INV-4），对用户如实降级（待研究草案 / degraded 标记），绝不冒充成功。
- **业务面**：违反业务规则是**异常路径**。抛 `app/common/envelope.py` 的 `ApiError(status, message)`——status 既作 HTTP 状态码也作 body.code，由全局 handler 统一包成 `Result{code,message,data}` 信封。业务路由里不要 try/except 吞成 200。
- 判据：这条失败**有没有替代产出**？有（降级草案、缓存回退）→ agent 轨；没有（权限、校验、不存在）→ 业务轨。

## 边界（门禁机检，违反即红）

- **分层**：`app.api → app.services → app.agent`，禁反向与跨层（import-linter 契约 1）。
- **agent 域阶梯**：agent 层内按域分（`core → runtime → data → grounding → tools/research → generation → editing`），上层可 import 下层，禁反向；未域化的平铺模块视为上层。域地图与"新代码放哪"落位表见 `app/agent/README.md`，机检 = import-linter 的 "Agent domain ladder" 契约 + `tests/test_agent_domain_ladder.py`。
- **agent 门面**：api/services 只准 `from app.agent import X` 用 `app/agent/__init__.py` 的导出面；深路径 import agent 子模块即红（契约 2）。新增对外能力 = 在 `__init__.py` 登记 re-export + `__all__`。
- **跨模块共用符号**：agent 层内多模块共用的内部函数由所属模块去下划线提级为「跨模块 API」并在模块 docstring 登记（见 generators/day_stream/tools/workflow）；不搞第二份实现，也不靠门面转发内部符号。
- **工具面**：全部工具经 `app/agent/tools/registry/` 注册表包派发（预算/参数校验/审计全覆盖），禁 `getattr(tools, name)` 字符串派发。注册 handler 与调用方一律 `from app.agent.tools import impl as tools` 后在**调用期**读 `tools` 模块属性（晚绑定），保 `patch.object(tools, ...)` 可 mock——实现模块叫 `impl` 是为了让域包 `tools/` 与"被 patch 的那个模块对象"仍是同一个。

## 数据访问选址（新增读写先选对门）

- **业务写**（行程/用户状态的写路径）→ `app/services/`（版本快照、缓存失效在 service 层做）。
- **agent 读**（地点事实查询）→ `app/agent/data/places.py`（OTM 半径池/点名兜底/地图深链）与 `app/agent/data/web_search.py`（联网补池）；本地知识库（原 `poi_repository.py`）已随 AI-NATIVE 数据面退役，勿再引用。
- **用户域** → `app/common/user_repository.py`。
- **缓存**（Redis / 进程内）→ `app/common/cache_store.py`（键命名与 TTL 对齐既有约定）。
- 不在路由、图节点、生成器里裸写 SQL。

## 命令

根目录 `just check` 等价于下面第一条（justfile 的配方只做委托，不重复步骤）。

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1  # 一键门禁（= CI preflight + 离线测试）
# 单项（check.ps1 的组成，同序）：ruff check . / ruff format --check . /
#   python scripts/typecheck.py（pyright 基线 ratchet，--update 只减不增）/
#   lint-imports（分层与门面边界）/ secret scan / 离线 pytest / 契约导出漂移
uv run pytest tests/ -q --ignore=tests/api --ignore=tests/perf  # 离线测试（含 tests/agent_eval 度量层单测）
uv run python scripts/export_contracts.py        # 改 schemas 后导出契约（产物入仓）
uv run python tests/agent_eval/eval_agent.py     # 离线评测（行为改动后对比）
uv run python tests/agent_eval/eval_editing.py   # 编辑链路离线评测
uv run python tests/agent_eval/eval_experience.py # 体验题集 E01-E14（fail>0 拒出报告即红）
uv run python scripts/eval_ratchet.py            # eval 指标棘轮（跑完三套 offline eval 后对照基线）
uv run pytest tests/api -q                       # 活栈契约（需先起服务）
```

行为防线（G-2.0，CI 的 `agent-eval` job 同款）：

- **流式快照**：`uv run pytest tests/test_stream_snapshot.py`——trip/day 两条链路的
  事件序列/DailyPlan 与 `tests/golden/stream_*.json` 逐字节比对。有意改口径：
  `GOLDEN_REGENERATE=1 uv run pytest tests/test_stream_snapshot.py`，**人工复核 diff 后**入库
  （同法适用于 `GOLDEN_REGENERATE=1 uv run pytest tests/test_format_output_golden.py`
  重写 `tests/golden/format_output.json`）。
- **eval ratchet**（PR-0 口径，取代报告字节比对）：跑 `eval_agent.py` + `eval_research.py` +
  `eval_editing.py` 后
  `uv run python scripts/eval_ratchet.py`——逐指标对照 `tests/agent_eval/metrics_baseline.json`
  双向棘轮：回归红；显著变好也逼 `--update` 认账收紧（基线只跟不松）；未冻结的新指标红。
  字节比对区分不了变好/变坏，被棘轮 + CI `eval-determinism` job（同 fixture 两遍报告
  SHA256 必须一致）取代；离线护栏（实时价/联网入口哨兵）保证报告确定可复现。指标口径见
  `report/offline/report.md`。报告分四类：`report/offline/`（mock 离线，进棘轮）、
  `report/experience/`（体验题集 E01-E14 的业务路径指标，`eval_experience.py` fail>0 拒出报告
  即红，报告随代码入仓、不进棘轮）、`report/nightly/`（真实 LLM，`eval_gate.py` 防倒退下限，
  不进棘轮、可被 nightly 重跑覆盖）、`report/live/`（授权活栈验收：业务路径同题 p50/p95、
  clarify 人工评分素材与 LLM 裁判辅助报告，由 `scripts/live_perf_probe.py` 生成，随代码入仓）。
  **报告重生成流程**（eval 报告没有 GOLDEN_REGENERATE 等价开关）：改用例/改指标口径后
  直接重跑两个脚本，`report/offline/report.json|md` 与 `report/offline/research_report.json|md`
  就地重写；指标有意变化跑 `scripts/eval_ratchet.py --update` 重落基线（放宽界值属 INV-1
  豁免，PR 须留注释与期限），**人工复核 diff 后与代码同一 commit 入库**。题集
  （`cases.json` / `themed_cases.json` / `replay_cases.json`）受 `tests/agent_eval/schemas/datasets.schema.json`
  校验（加载点统一走 `dataset_schema.load_dataset`），报告记数据集哈希——改题集同样是一次
  认账。体验题集独立成套：`tests/experience_cases/`（E01-E14，`e_dataset.json` 带
  schema/版本/hash，由 `tests/experience_cases/runner.py` 驱动，不得让指标脚本与测试分别
  手抄题目），改口径走 `eval_experience.py` 的 fail>0 拒出报告门。cases 可带 `"coords": false` 走无坐标边界变体
  （C3.2；海外城市直接用非汉字名，mock 坐标按城市名落国内框内/海外）。
  深度指标口径（C3.2）：`coord_valid_rate`=attraction/food/hotel 项坐标有效率
  （0/0 与 None 为缺失哨兵）；`deeplink_resolvable_rate`=按天计的全天路线深链可解析率
  （≥2 个有效坐标停靠点的天必须可解析，不足 2 点自动通过；后端 places 语义，前端
  `mapPins.ts` 的 `hasValidCoords` 同谓词、由 `mapPins.test.ts` / `deeplink-parity.test.ts` 覆盖）；`category_reasonable_rate`=item_type
  白名单 + poi_name 非空。真实 LLM 侧同名指标进 `eval_gate.py` 防倒退下限
  （`depth_metrics`，仅 nightly 生效）。eval_gate 下限 2026-09-23 按流式小样本重认账
  （`--path stream --limit 3` × 2 遍，qwen-plus）：一致性 16.67%→0%、深度
  0.80/0.80/0.95→0.728/0.2222/1.0 含**放宽侧**，按 INV-1 记豁免注释于此；口径 =
  stream 路径 + 本地缺 city_geo 的降级环境（nightly 全量迁移环境预期只高不低），
  期限 = 下一次同口径小样本重认账时复核（稳定性改进后只准上调）。
- 改 P2 拆分时先跑这两条：单测断言单点字段，它们断言**端到端行为与事件序列**。

配置：`app/common/config.py` 是 **pydantic-settings 字段定义式**（键名小写即环境变量名，如 `agent_host` ↔ `AGENT_HOST`）；新增键 = 加字段 + 同 PR 更新 `.env.example`（`tests/test_env_example_alignment.py` 会验）。启动校验在 `Settings.validate_boot()`（main.py lifespan 调）；依赖运行语境的安全检查（密钥强度、绑定地址）只放这里，不放 import 期。
