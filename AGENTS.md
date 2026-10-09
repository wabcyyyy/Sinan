# AGENTS.md — Travel-Assistant

## 项目是什么（3 句话）

AI 旅行规划 Agent：输入城市/天数/偏好/一句话意图，LangGraph 工作流（parse→research→generate→reflect→format）生成 AI 规划的每日行程（关键事实标 estimated，前端挂地图深链引导出发前核实），SSE 实时推送进度。
后端是**单一 FastAPI 进程**（`travel-agent-python/`，Python 3.12 + uv）：业务面 `/api/**` 与 Agent 面 `/api/agent/v1/**` 同进程直调；**AI-NATIVE 数据面（2026-09-17）**：本地不存景点语料（V4 已退役 `poi_knowledge`），地点事实 = LLM 世界知识 + 联网搜索 + OpenTripMap（坐标/分类/图片）+ Nominatim（点名→坐标兜底），数据库只存用户数据与城市级字典/消费基准。
前端入口是 React 19 + Vite + TS 壳（`travel-frontend-react/src/react-app/`，2026-09 起对话式创建：首页 ChatIntake 一句话收集槽位、详情页 ChatPanel 对话编排），MapLibre + OpenFreeMap 免 key 底图不变；Vue 组件树已整体删除（目录 2026-10-04 更名 travel-frontend-react）。

## 命令（在指明的目录下执行）

装了 [just](https://github.com/casey/just)（`uv tool install rust-just`）后，根目录的
`just dev / just test / just check / just eval / just snapshot / just fe-check` 是入口；
没装 just 时下面这些原命令照旧有效。

```bash
# 一键门禁（改任何 Python 代码后跑这个）：ruff check → ruff format --check →
# pyright 基线 ratchet → import-linter 边界 → 代码规模 ratchet → secret scan →
# 离线测试(+覆盖率) → diff-cover 变更行覆盖率 → 契约漂移；与 CI preflight 同序同果
cd travel-agent-python && powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1

# 契约导出（改了 app/schemas/ 后必须跑，产物入仓，CI 校验漂移）
cd travel-agent-python && uv run python scripts/export_contracts.py

# 离线评测（mock 驱动，无需 LLM key；行为类改动后跑全套再对照棘轮基线）
cd travel-agent-python && uv run python tests/agent_eval/eval_agent.py
cd travel-agent-python && uv run python tests/agent_eval/eval_research.py
cd travel-agent-python && uv run python tests/agent_eval/eval_editing.py
cd travel-agent-python && uv run python tests/agent_eval/eval_experience.py
cd travel-agent-python && uv run python scripts/eval_ratchet.py

# 前端构建与门禁
cd travel-frontend-react && npm run build && npm run test:unit && npm run theme:lint && npm run ep:lint
```

## 跨仓约定

- **契约单一源**：跨端线级模型全部定义在 `travel-agent-python/app/schemas/**`（登记表 `app/schemas/contracts.py` 的 CONTRACT_GROUPS：agent_api + business_api；事件在 `stream_events.py`），`scripts/export_contracts.py` 据此导出 `contracts/*.schema.json` + `contracts/openapi.json` + 前端生成类型 `travel-frontend-react/src/types/generated/contracts.ts`（前端契约类型只准引它）。改 schema → 重新导出 → 产物随同一 commit 入仓；CI 与本地 check.ps1 都有 drift 门禁，手改 `contracts/` 或生成类型必红。
- **后端分层有门禁**：`app.api → app.services → app.agent` 三层，api/services 只准经 `app/agent/__init__.py` 门面用 agent 能力；由 import-linter 契约机检（`pyproject.toml` 的 `[tool.importlinter]`，入 check.ps1 与 CI）。agent 层内按域分（`core → runtime → data → grounding → tools/research → generation → editing`，域地图与落位表见 `travel-agent-python/app/agent/README.md`），契约按**域级**登记——已登记域内新增模块免登记，新增域或新增平铺模块必须登记。
- **前端消费面口径（2026-09-27 拍板）**：后端 104 paths（2026-10-08 实数）中有六域前端**不（全）消费**，这是产品阶段取舍、不是遗漏——expense（消费面是 MCP 记账，不做手工记账 UI）、collab（单人产品定位，后端设计保留）、version（后端保留；若未来补前端，最小切片=版本列表+一键恢复）、admin（运维经 curl/脚本直调，不做管理 UI）、templates（前端明确放弃，flag 默认关，sinan.ts 死函数已清）、feedback（C3.5 条目对/错 UI 已补齐，flag 默认关=前端探针自动隐藏）。各域后端端点与测试一律保留，不因前端不用而退役；翻案前先改本条。
- **Java 已退役**：`travel-backend-java/` 已删除，FastAPI 是唯一后端。不要以任何形式复活第二份后端实现；历史与恢复步骤只看 `ARCHIVED.md`。业务端点的行为基准是 `tests/api` 契约与 `tests/test_cutover_contract.py`。
- **数据库迁移 append-only**：SQL 真相源是 `travel-agent-python/app/db/migrations/sql/V*.sql`，已入库的迁移文件禁止修改/重排，新变更只能追加新版本。

## 哲学（违反任何一条的 PR 请先停下来说明）

1. **单一真源**：每类事实只有一份权威定义（schema 在 app/schemas、SQL 在 db/migrations、env 键在 app/common/config.py + .env.example）。副本必然漂移，不允许出现第二份。
2. **门禁只升不降**：静态检查、测试、eval 基线只许收紧。放宽门禁的改动需要在本文件留注释与期限。
   - 登记项（2026-09-30，前端错误探针）：`POST /api/client-errors` 加入 `test_default_deny_auth` 的匿名写豁免清单——登录页自身崩溃时拿不到会话，探针必须匿名可达。滥用面有界（只写日志、字段上限、按 IP 分钟窗 30 次）。期限：上线后首次日志复盘（不晚于 2026-10-31）复核；日志面出现灌水即改走强制会话。
   - 登记项（2026-10-08，eval_gate 深度下限降档）：stream 路径的 `coord_valid_rate` / `deeplink_resolvable_rate` 下限从 0.728 / 0.2222 暂降为 presence-only（0.0）。依据：三轮同口径实测 0.8504 / 0.5722 / 0.6754 + 一次 3 例样本 deeplink 反向触红——这两个率由**外部地理编码可用性（Nominatim 1rps、OSM 国内覆盖）**主导，下限冻结自单轮小样本，红绿随外部服务天气随机波动，分不清回归与噪声。结构检查保留（指标在、数值型、版本/正文指纹不变），观测值照常打印积累带宽。期限：攒够 ≥5 轮全量（13 例）同口径样本后按带宽数值重定下限，或改为多轮中位数门禁；`category_reasonable_rate`（1.0，生成质量属性、非外部）不受本项影响。
3. **代码赢过文档**：文档与代码冲突时，先修文档使其对齐代码事实；描述"将要做的重构"的文档不放仓库。
4. **接缝就地迁移，不加包装层**：改接口签名时直接迁移全部调用方，不保留兼容 shim/re-export 包装层。
5. **AI 写的必须有人读懂**：不引入没人能解释清楚的代码；新增依赖必须在 commit message 里给出理由。

## 分层细则

- Python 包：看 `travel-agent-python/AGENTS.md`（不变量、错误边界、数据访问选址）。
- 前端：看 `travel-frontend-react/AGENTS.md`（外观门禁、组件拆分）。
