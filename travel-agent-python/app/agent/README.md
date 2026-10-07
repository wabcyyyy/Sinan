# Agent 域地图与生成链路蓝图

> 本文件是 agent 层组织方式的**单一真源**：域地图回答"新代码放哪个域"，生成链路蓝图
> 回答"生成域内放哪一层"。阶梯部分由门禁机检（`pyproject.toml` 的 "Agent domain
> ladder" 契约 + `tests/test_agent_domain_ladder.py`），改结构必须同步改这里。

## 域地图（2026-09-23 域化改造完成）

阶梯单向：**上层可 import 下层，下层不得 import 上层**。八个域全部落地，`app/agent/` 下已无平铺模块（门面 `__init__.py` 除外）。

```
editing       自然语言入口：改行程 / 对话 / 问答                   已落地
generation    生成链路：造行程内容，域内再分四层（见下）            已落地
research      多 Agent 研究面                                     已落地
tools         工具面：注册表 / 派发 / FC 循环 / 工具实现            已落地
grounding     结论层：存在性判定 / 证据票 / 溯源标签                已落地
data          外部事实面：只取数据、不下结论                       已落地
runtime       运行期基座：轨迹 / 限额 / 用量 / 指标 / 会话状态       已落地
core          零依赖原语：json_utils / geo / poi_identity / intent  已落地
```

（阶梯是**线性**的：research 在 tools 之上、generation 之下——研究面消费工具面，
生成链路消费研究面。机检按这个线性次序判上/下层。）

落位表（"我要加 X → 放哪"）：

| 我要加… | 放 | 判据 |
|---|---|---|
| 一个纯计算 / 纯解析原语 | `core/` | 只准 stdlib / `app.common` / `core` 自身 |
| 一次运行边界（deadline、计数） | `runtime/run_limits.py` | 沿用既有 `check(kind)` 口径，不新开预算通道 |
| 一次 agent run 的图状态（day/trip/research 共用） | `research/agent_state.py`（`UnifiedAgentState`，全仓唯一） | 节点一律属性访问、无兼容访问器；落 research 是因为状态携带 `ResearchTask`/`EvidencePack` 域内类型（PR-2） |
| 一份跨模块共享的计数状态 | `runtime/tool_budget.py` | 开/占用分离：observability 开、消费方就地自增 |
| 一条轨迹事件 / 一个指标 | `runtime/trace.py` / `runtime/observability.py` | 不新开采集通道 |
| 一类外部数据源（HTTP / DB / 第三方插件） | `data/` | 只取数据、不下结论；失败如实降级不冒充成功 |
| 一条"城市 → 坐标 / 点位 → 地址"的解析链 | `data/`（城市中心是 `data/city_center.py`） | 唯一入口，别在消费方各写一份兜底 |
| 一类"这个点位是否存在 / 是否同一"的判定 | `grounding/`（同一性判定在 `core/poi_identity.py`） | 证据票与溯源标签只准有一份实现 |
| 一个新工具 | `tools/` + 登记进 `tools/registry/catalog.py` | handler 调用期读 `tools` 模块属性（保 mock） |
| 一次 LLM 调用入口 | `generation/content/day_prompts.py`（对话 / 编辑侧见 `editing/`） | 未配置即抛、失败向上 |
| 一条编排分支 / 一个图节点 | `generation/orchestration/` | 节点不抛，一律路由到 fallback |
| 一类用户自然语言指令的解析 | `editing/` | 解析成结构化操作，不全量重生成 |

域内模块只准 import 同域或更下层的域；平铺模块不在阶梯里，域内 import 它们需要
在 `tests/test_agent_domain_ladder.py` 登记豁免（并写明消除条件）。

## 生成链路模块蓝图（G-2.1）

> 本文件是 G-2.2~2.4 拆分的**验收依据**：只定义形状与依赖方向，不列清单。
> 新增生成能力时先读这里，再决定放哪一层。
> 生成域已迁入 `generation/`，域内四层：`rules → content → output → orchestration`
> （层序由依赖图 SCC + 拓扑排序算出；机检 = pyproject 的 "Generation sublayers" 契约）。
> 下表的平铺名对应关系：`generation_core`/`budget`/`transfer_time` → `rules/`（转场判据与生成后微调，2026-10-08）；`generators`/`suggestions`/
> `reference_pool`/`narrative`/`day_prompts`/`landing` → `content/`；`reflect`/`route_matrix`/
> `graph_state` → `content/`（`graph_state` 已随 PR-2 并入 `research/agent_state.py` 的
> `UnifiedAgentState`，全仓唯一图状态定义）；`formatting/`（assembly/facts/prices/quality）/`critic`/
> `schedule_optimizer` → `output/`；其余编排模块 → `orchestration/`。
>
> 注意蓝图里"formatting 是 workflow 的下游"指**执行顺序**，不是 import 方向：
> import 方向是 `orchestration → output`（编排层调用它装配行程）。

## 一句话职责

| 模块 | 一句话 |
|---|---|
| `generation_core.py` | 纯规则骨架：N-1 晚住宿节奏、摊铺、预算估算、脏项与模型申报字段清洗；每日入住排程委托同域 `hotel_schedule.py`，不 import 内容或编排层 |
| `hotel_schedule.py` | 纯住宿排程：沿用酒店实体与报价，保留每天入住时间；漏排晚次接在当天活动后，午夜为上界 |
| `core/poi_identity.py` | 点位同一性判定：名称归一、球面距离（0/0 判缺失）、跨天去重——域阶梯最底层，被生成与接地两侧共用 |
| `budget.py` | 预算档位与约束句、餐价钳制、0 价补水 |
| `suggestions.py` | 备选池：候选组装、类别地板与上限、缺口补齐 |
| `suggestion_grounding.py` | 备选池的批量后验证（「发现更多」那 24-40 个名字唯一一次被外部数据检验） |
| `reference_pool.py` | 参考资料池：把权威候选编成可引用的编号资料，并把模型引用落地回权威字段 |
| `landing.py` | 两条生成链路共用的落地步骤：脏项过滤 → 引用/本地落地 → 证伪点删除；单日 `drop_used_items` 在接地后复用 PoiSeenRegistry 去重 |
| `existence.py` | 存在性解析器：可插拔 provider 的三值判定（证实/证伪/未判定）+ 同城硬闸 + 证据票签发 |
| `existence_commercial.py` | 付费 provider（高德 / Google Places）：无 key 一律如实未判定，不占免费配额 |
| `grounding_evidence.py` | 证据票：「本服务真的抓过这一行」的不可伪造凭据 |
| `grounding_labels.py` | 背书口径唯一实现：来源值域 + 证据校验 → 溯源标签（三条链路共用） |
| `grounding/facts.py` | 事实落地：坐标校验与本地权威补点（判定委托 `existence`） |
| `map_link.py` | 地图深链：关键词/路线两种 URL 与 GCJ-02 偏移 |
| `open_plans.py` | 图路径的逐日草案：补池 → 落地 → 删证伪点 → 组装（`on_day`/`on_patch` 流式挂点在此） |
| `generators.py` | 内容组装原语与对外面（纯函数优先，无编排） |
| `narrative.py` | 叙事字段清洗及最终排程概述：限值、类型、指令残留与模型申报字段清洗；theme/note 由 items 派生 |
| `day_prompts.py` | Prompt 组装：系统提示绑定、约束句拼装、日/整段两种入口的 LLM 调用 |
| `json_output.py` | 生成输出骨架校验与携原请求的单次修复；空行程明确失败 |
| `suggestion_quality.py` | 交通线路与旅游设施的语义区分；备选池各补齐路径共享准入检查 |
| `day_stream.py` | 单日链路：一次生成的编排与落地（反思/重试不在此） |
| `day_workflow.py` | 单日图的门面 |
| `stream_branch.py` | 整段流式：统一图的流式分支，custom stream 逐天下发 NDJSON 事件（`run_generate_trip_stream`） |
| `workflow.py` | 图工作流的门面：生成 → 校验 → 修复循环 |

## 依赖方向（禁反向）

```
stream_branch ─┐
day_stream ────┼─→ workflow ──→ generators ──→ generation_core / core/poi_identity
               │      │            │  │  │
               │      │            │  │  └─→ budget / suggestions / reference_pool
               │      │            │  └────→ narrative / grounding / day_prompts
               │      └───────────────────→ formatting / reflect
               └──────────────────────────→ research（只读证据）
```

- **自上而下单向**：`stream → workflow → generators → generation_core`。上层可以调用下层，下层**不得** import 上层。
- `stream` 层（stream_branch / day_stream）负责编排与事件；`workflow` 负责图与循环；`generators` 及其兄弟模块负责"造内容"；`generation_core` 只做规则。
- `formatting/`（价格/事实/质量）与 `reflect.py`（校验）是 workflow 的**下游**，不得反过来 import 生成模块。
- 事件模型只认 `app/schemas/stream_events.py`：事件构造 → `to_wire`，别在业务代码里手搓键名。

## 参考实现

- **编排与降级**：抄 `research/`——子任务边界清晰、失败响亮降级、全程 trace 事件。
- **纯规则模块**：抄 `generation_core.py`——不 import 兄弟模块、函数式、可单测。
- **薄门面**：抄 `day_workflow.py`——只做转发与导出，不承载逻辑。
- **可 mock 性**：抄 `tools/registry/` 的 handler 约定——被 patch 的函数经模块属性（`from app.agent.tools import impl as tools`）在调用期解析。

## 新增生成能力先抄谁

1. **新增一个纯计算规则**（住宿节奏/摊铺/折算）→ 放 `generation_core.py`，抄它的无依赖风格。
2. **新增一类"是不是同一个点位"的判定** → 放 `core/poi_identity.py`：名称归一与距离门槛只准有一份，否则去重与存在性判定会各说一套。
3. **新增一个约束句或档位** → 放 `budget.py` / `day_prompts.py`，抄 `budget_clause` 的"空输入返回空串"契约。
4. **新增一类备选池条目** → 放 `suggestions.py`，抄它的类别上限与地板守卫。
5. **新增一次 LLM 调用入口** → 放 `day_prompts.py`，抄 `llm_open_day` 的"未配置即抛、失败向上"口径。
6. **新增一条编排分支** → 放 `workflow.py` 的门面函数，抄 `day_workflow` 的图编排与 fallback 路由（节点不抛，一律路由到降级节点）。

## 拆分纪律

- **等积拆分，只搬不改**：函数体逐行搬移，不顺手"优化"；行为判据是流式快照（逐字节）与 eval ratchet（指标不降）。
- **不留包装层**：改换归属就直接迁移全部调用方，不保留 re-export 兼容壳（门面模块除外——门面本身就是公开面）。
- **可 mock 契约不许破**：把函数搬到新模块时，同步迁移测试的 `patch.object(<模块>, "<名字>")` 目标，保持"调用期解析"语义。
