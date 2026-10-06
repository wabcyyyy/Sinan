# travel-agent-python

**单进程后端 + AI Agent 服务**：既提供业务接口（`/api/**`：鉴权、行程读写、版本快照、PDF 导出、后台管理、SSE 事件流），也提供 Agent 接口（`/api/agent/v1/**`）。行程生成/调整通过 LangGraph 工作流调用 LLM（任意 OpenAI 兼容端点，默认 DeepSeek 官方 `deepseek-flash`）。**AI-NATIVE 数据面（2026-09-17 起）**：本地不再维护景点语料库（`poi_knowledge` 已随 V4 迁移退役）——地点事实走「LLM 世界知识 + 联网搜索（DashScope enable_search）+ **OpenTripMap**（OSM+Wikidata 加工的旅游切片：坐标/分类/图片/百科简介）+ **Nominatim**（点名→坐标兜底）」，票价/营业时间无结构化来源，由 LLM 估价并按 `estimated` 如实标注，前端以**地图深链**（国内高德 URI / 海外谷歌 Maps）引导用户出发前核实。数据库只存用户数据：用户、偏好、行程快照（`itinerary_item` 为快照式落库，已生成行程不依赖任何外部数据源）、`city_geo` 城市字典与 `city_consumption` 消费基准。（历史上业务面在 Spring Boot 侧，已按绞杀者路线整体迁到本服务，两个面同进程直调。）

生产主链路为**整段流式生成**（`/v1/generate-stream`）：一次 LLM 调用生成整趟行程（模型看得见全盘，跨天重复与片区走回头路在源头收敛），流式增量解析 `daily_plans` 数组，每解析出一个完整天立即做引用落地/坐标补齐/双通道去重（归一化同名 + 落地坐标 <80m），以 JSON Lines 逐天下发；`/v1/generate`（整段非流式，走同一张 LangGraph 图）用于直接调用与离线评测，`/v1/generate-day` 供编排器做恢复续跑与缺天修复。

## 技术栈

- Python 3.12（uv 管理依赖）
- FastAPI 0.111 + Uvicorn
- LangGraph 0.2.44 + LangChain Core
- PyMySQL（读 `city_geo` 字典与 `city_consumption` 消费基准两张城市级表）
- 外部地点层：OpenTripMap（免 key 注册；仅 en/ru，名称为英文，中文名由 LLM 对齐）+ Nominatim（免 key，1 rps 政策）
- 联网搜索：DashScope enable_search 插件（经 LLM 供应商，补真实店名/项目名；不保证坐标）。**只有百炼兼容层有这个插件**：主通道若是 DeepSeek 官方等无插件网关，联网补池与实时价会如实按「未联网」降级（一次外呼都不发，绝不用模型记忆冒充检索结果；见 `app/agent/data/web_search.py:search_channel_ready`）。要保留联网能力就把该通道指向 `https://dashscope.aliyuncs.com/compatible-mode/v1`。

## 工作流（LangGraph）

```
parse ──► research ──► generate ──► reflect ──► format
                 │                       │
                 └───── 失败/重试/补查 ───┘

research 节点（多 Agent 研究层）：Supervisor 并行派发 酒店/景点/美食 三个研究 Agent
  （LLM 规划检索策略 → 工具检索 → 评估充分性 → 不足补查一轮），输出证据包 EvidencePack；
  校验发现"无景点"类证据缺口时只重派发对应研究 Agent 补查。
  工具检索 = OpenTripMap 半径池（景点，带坐标）∪ 联网搜索补池（真实店名，不保证坐标）。

generate 内部（LLM-only，外部数据只作证据）：
  开放模式（唯一路径）：LLM 知识选点 + 候选资料[Rn]注入 → refs 引用落地 → Nominatim grounding 补坐标
  失败降级：           重试一次 → 仍失败返回"待研究草案"（绝不以候选拼装行程冒充生成结果）
```

- `parse`：意图解析（城市/天数/偏好/预算）
- `research`：多 Agent 研究编排 —— 三域研究 Agent 并行收集证据（OTM 半径池 + 联网搜索补池），Supervisor 汇总证据包并驱动证据型缺口补查
- `generate`：开放模式为主；外部候选（OTM/Nominatim/联网）命中项落地为 `partially_verified/observed` 并回填真实 `source`，模型自选点标 `unverified/before_departure`（坐标由 Nominatim grounding 补齐，解析不到的城市如实留空）
- `reflect`：校验时间冲突 / 路线可达性 / 日饱和度 / 预算超支等 11 类规则，`needs_fix` 时最多 2 轮修正，输出 `validation_log`
- `format`：结构化输出 + 确定性预算重算（成本随草稿/估价落地，酒店走估价→季节系数定价链）

每次外部检索写入脱敏 Trace 事件（含候选数、来源与 fallback 标记）；可通过 `X-Agent-Run-ID` 追踪完整检索链路。

## 外部地点层（app/agent/places.py）

- **OpenTripMap**（主源）：城市定位（geoname）→ 分类半径检索（坐标/分类/热度，`sort=-rate`）→ xid 详情（结构化地址/官网/图片/维基简介）。进程内 TTL 缓存（城市 24h / 半径 6h / 详情 24h），负结果短 TTL；`OTM_API_KEY` 为空时整层禁用，景点池降级为纯联网搜索
- **Nominatim**（兜底）：任意名称→坐标，OTM 语义上兜不了"点名解析"由它承担；公共实例 1 rps，`ExternalClient` 按 1.1s 间隔自节流
- **地图深链**（纯函数）：`map_search_url` 单点核实链接、`map_directions_url` 全天路线链接——国内高德 URI / 海外谷歌，坐标判定国界框、无坐标按城市名汉字启发
- **事实边界**：两家都没有票价/营业时间；这些字段由 LLM 估价（`value_kind=estimated`），绝不伪造权威来源

## 目录结构

```
main.py                # 入口（启动时自动迁移 schema 并导出 openapi.json）
app/
├── api/               # agent.py（Agent 面 /api/agent/**）+ business/（业务面 /api/**：鉴权/行程/图片/导出/后台）
├── agent/             # workflow / research(多Agent研究层) / trip_stream(整段流式生成) / day_stream(开放模式事实层) / generators(ReferencePool) / reflect / tools / places(OTM+Nominatim+深链) / city_reference(城市字典/消费基准) / tool_registry / observability
├── services/          # 业务域服务（行程读写/版本快照/生成编排/缓存/PDF 导出/图片代理）
├── db/                # ORM 模型 / 启动即迁移执行器 / migrations/sql（全仓唯一 SQL 真相源；V4 退役 poi_knowledge/hotel_room_type）
├── common/            # config(.env) / llm_client / external_client(外部取数基类) / task_pool / event_hub / redis_client / season
└── schemas/           # trip（含 FactEvidence/SourceRecord 溯源模型）/ stream_events（SSE 事件契约）/ agent_ops / common
tests/
├── test_*.py          # 离线单测（数量随迁移推进增长，以 CI 为准；覆盖引用落地/跨天双通道去重/流式解析/白名单/反思/安全编辑/多 Agent 研究/SSE 与对话改行程/PDF 导出/后台管理/启动即迁移/审计修复回归）
├── api/               # 接口自动化（打本服务活栈，默认 127.0.0.1:8000，API_BASE_URL 可覆盖）
├── agent_eval/        # 研究/Agent/LLM 三套评测 + 失败回放 + 基线消融
└── perf/load_test.py  # 性能压测
```

## 启动

```bash
cp .env.example .env   # 填入 LLM_API_KEY、DB_PASSWORD、JWT_SECRET（≥32 位，本服务签发会话票）；OTM_API_KEY 建议配置（opentripmap.io 免费注册）
uv sync                # 安装运行时依赖
uv run python main.py  # 127.0.0.1:8000，默认不开启 reload
```

环境变量（`.env`）：`LLM_BASE_URL`（默认 DeepSeek 官方 API `https://api.deepseek.com`）、`LLM_API_KEY`、`LLM_MODEL`（默认 deepseek-flash）、`LLM_ENABLE_THINKING`（思考模式开关，DeepSeek 官方通道下发 `thinking: {"type":"enabled"|"disabled"}`）、`LLM_REASONING_EFFORT`（思考强度 low/high/max；留空 = 不下发，空串等同没配）、`LLM_TIMEOUT`（批量生成 200-300 字介绍时输出可达 4000+ token，建议 ≥240）、`DEFAULT_BUDGET`、`DB_*`、`AGENT_INTERNAL_TOKEN`（配置后保护生成/编辑接口；`AGENT_HOST` 绑定非回环地址且未配置 token 时启动直接报错，留空仅适合本机回环开发）、`AGENT_RELOAD`（开发环境可设为 `true`）。**模型侧已角色化（2026-10-04）**：一个用途一个角色键 `LLM_ROLE_{MAIN,FAST,VISION,JUDGE,SEARCH,STT}`，值是 `provider:model`（裸模型名 = 默认 provider，留空 = 复用 main）；额外网关按名册 `LLM_PROVIDER_{DASHSCOPE,MIMO}_{BASE_URL,API_KEY}` 声明。解析、能力判断与启动期校验都在 `app/common/model_registry.py`——新增用途不必再加一组平铺键。外部地点层：`OTM_API_KEY`（opentripmap.io 免费 key；留空则景点池走纯联网搜索）、`OTM_RADIUS_M`/`OTM_LIMIT`（城市半径检索范围与上限）、`PLACES_TIMEOUT_SECONDS`（OTM/Nominatim 单次超时）、`NOMINATIM_ENABLED`（点名解析兜底开关；公共实例 1 rps 政策由客户端自节流保证）。表结构由本服务启动时自动迁移（`app/db/migrate.py`：空库按版本序执行 `app/db/migrations/sql/V*.sql` 全量建表，已有表的库只打版本点、绝不重跑 DDL），无需手工执行建表/增量 SQL；SQL 真相源在 `app/db/migrations/sql/V*.sql`（全仓只留这一份，V4 退役了 `poi_knowledge` 与 `hotel_room_type`）。若配置 `ROUTE_SERVICE_ENABLED=true`，反思与质检阶段会用确定性路线矩阵校验 POI 间可达性——纯坐标估算（haversine × 道路系数 + 缓冲），无任何外部路线 API，结果自带 `degraded` 标记。`SCHEDULE_OPTIMIZER_ENABLED=true` 控制确定性时间窗优化器。`TOOL_MAX_CALLS` 控制单次 Agent 请求的工具调用总预算，单工具风险、版本、Schema、超时和调用上限由 Tool Registry 管理；`AGENT_DEADLINE_SECONDS`、`MAX_LLM_CALLS`、`MAX_TOKEN_BUDGET` 和 `MAX_REPLANS` 控制单次运行边界，`TRACE_STORAGE_PATH` 控制 JSONL Trace 持久化位置（错误文本中的 API key/Bearer 令牌会集中脱敏）。

## 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/agent/hello` | 健康检查 |
| `POST /api/agent/v1/generate-stream` | 整段流式生成（JSON Lines：start / day / day_patch / suggestions / done / error 逐天事件，事件体 camelCase） |
| `POST /api/agent/v1/generate` | 生成行程（城市/天数/偏好/预算 → 每日计划 + 预算明细 + validation_log + `status`；直接调用与离线评测用） |
| `POST /api/agent/v1/adjust` | 增量调整（替换/删除/排序 + 预算重算推荐） |
| `POST /api/agent/v1/generate-day` | 单日生成（恢复续跑 / 缺天修复；开放模式 + 候选资料注入，外部数据只作证据） |
| `POST /api/agent/v1/plan-context` | 一次性构建检索上下文（候选/餐饮/酒店/消费系数） |
| `POST /api/agent/v1/poi-nearby` | 同城近邻推荐（OTM 半径检索；锚点解析不到返回空，不伪造） |
| `POST /api/agent/v1/poi-intros` | 景点详细介绍批量生成 |
| `POST /api/agent/v1/city-guide` | 城市引导对话（不确定去哪时按偏好推荐） |
| `POST /api/agent/v1/chat-turn` | 行程对话编辑（封闭动作集决策） |
| `POST /api/agent/v1/replan-local` | 按受影响日期、锁定点和候选池执行局部重规划 |
| `GET /api/agent/v1/metrics` | 进程内运行指标（成功率/降级率/失败率/token 分场景/工具调用） |
| `GET /api/agent/v1/metrics/prometheus` | Prometheus 文本格式导出 |
| `GET /api/agent/v1/tools` | 已注册工具的 Schema、风险、超时和调用预算（需内部 token） |
| `GET /api/agent/v1/runs/{run_id}` | 按 `run_id` 查询脱敏 Trace（内存最近 100 条 + JSONL 持久化回放） |

### 业务接口（`/api/**`，由 Spring 侧整体迁入）

各 router 自带完整前缀，便于按路径前缀灰度与回滚；鉴权是 **router 级默认拒绝 + 显式白名单**（对齐 Spring `anyRequest().authenticated()`），响应保持 `Result{code,message,data}` 信封。

| 域 | 代表端点 | 说明 |
| --- | --- | --- |
| 鉴权 / 用户 | `POST /api/auth/{register,login,logout,logout-all}`、`GET /api/user/info` | HttpOnly Cookie(`TA_AUTH`) + Redis 吊销黑名单；登录注册限速 |
| 偏好 | `GET/POST /api/itinerary/preferences`、`POST/GET .../preferences/signals` | 用户偏好与行为信号 |
| 行程读 | `GET /api/itinerary`、`GET /api/itinerary/{id}` | 列表 / 详情（叙事、预算、溯源字段），详情带 Redis 缓存与精确失效 |
| 行程写 | `POST /api/itinerary/{generate,nl-edit,apply-plans,hotel-option}`、`PUT /api/itinerary/items/{itemId}`、`DELETE /api/itinerary/{id}` | 每次写都落版本快照；`/versions` 支持 diff 与回滚 |
| 对话与实时 | `GET /api/itinerary/{id}/events`、`POST /api/itinerary/{id}/chat-edit`（及 `/stream`）、`GET/DELETE /api/itinerary/{id}/chat-history` | 进度事件流（进程内 SSE，15s 心跳、每行程 5 连接上限）与对话改行程（阻塞 + 流式） |
| 对话输入 | `POST /api/image-intent`、`POST /api/speech-intent` | 图片 / 录音 → 一句话回填输入框（**交用户编辑后发送，不自动发送**）；图片走 `LLM_ROLE_VISION`，语音走 `LLM_ROLE_STT`（服务端 ASR，MiMo）；格式白名单 + 体积上限 + 配额闸 |
| 图片域 | `GET /api/poi-photo`、`GET /api/image-proxy` | POI 实景图解析（维基/图库链，未命中 404）后 302 到同源代理；代理只代取白名单图床（SSRF 防护） |
| PDF 导出 | `POST /api/export/pdf/{itineraryId}`、`GET /api/export/tasks/{taskId}`、`GET /api/export/download/{taskId}` | 异步任务 + reportlab 排版；下载是文件流，不套信封 |
| 后台管理 | `GET /api/admin/{stats,users,itineraries,agent-metrics,llm-usage}`（+ `PUT /users/{id}/status/{status}`、两处 `DELETE`） | 需 admin 角色，非管理员 403 / 匿名 401 |
| 连通性 | `GET /api/test/hello`、`GET /api/agent/health` | 一键启动脚本与 CI 流水线的探活目标 |

完整清单以入仓产物 `contracts/openapi.json` 为准（由 `scripts/export_contracts.py` 单点导出，CI 与本仓 `scripts/check.ps1` 的漂移门禁逐字节看护）。启动期**不**再落 `openapi.json` 到根目录——第二份产物必然与入仓那份漂移。

端点面与调用点的一致性由 `tests/test_cutover_contract.py` 机检（前端调用点 + 活栈测试路径 vs 装配后路由）；历史上的 `scripts/check_endpoint_coverage.py` 已在 Java 退役后变成恒真门禁，2026-09-18 删除（理由见 `tests/test_retirement_readiness.py` 模块 docstring）。

## 测试

```bash
# 离线单测与 Agent 评测（零外部依赖：DB/模型/外部 API 全走替身）
uv run pytest --ignore=tests/api -q
uv run python tests/agent_eval/eval_agent.py
# 报告输出：tests/agent_eval/report/offline/report.json / report.md（进 eval_ratchet 指标棘轮）
uv run python tests/agent_eval/replay.py
# 回放未知 POI、路线不足、营业时间越界等安全失败案例

# 接口自动化冒烟（需本服务运行中；默认打 127.0.0.1:8000，API_BASE_URL 可覆盖）
uv run pytest tests/api -q

# 性能压测（需本服务 + Redis 6380 运行中）
uv run python tests/perf/load_test.py --endpoint all --duration 10 --workers 20
# 报告输出：tests/perf/report/load_report.json / load_report.md

# 切流量契约门禁（前端调用点 + 活栈测试路径 vs 装配后路由；离线套件里自动执行）
uv run pytest tests/test_cutover_contract.py -q
```

## 离线评测指标（当前工作树可复现）

固定 fixture 评测覆盖候选引用、事实字段引用、时间冲突、跨天重复、预算一致性、fallback 成功率和轨迹完整率；路线/优化器消融脚本 `tests/agent_eval/eval_baselines.py` 使用固定 `fixture-route` 替身对比坐标基线与路线优化基线，报告写入 `tests/agent_eval/report/offline/baseline_report.md`。检索质量评测（Recall/MRR/NDCG 等）随语料库退役一并移除——外部数据源的质量由其自身服务等级保证，本地不再建索引也不再评测索引。真实 LLM 指标需在固定模型、temperature、Prompt 版本后单独生成，不能直接复用 fixture 报告。行为防倒退判据（PR-0 口径）：`scripts/eval_ratchet.py` 逐指标双向棘轮（基线 `tests/agent_eval/metrics_baseline.json`）+ CI `eval-determinism` job 两遍报告 SHA256。

真实 LLM 双跑评测：

```bash
# 必须配置真实 LLM_API_KEY；脚本会固定当前模型、temperature（day_prompts.GENERATION_TEMPERATURE）、
# Prompt 版本，逐遍记录 run_id、脱敏 Trace、token、调用/重试/fallback 和失败原因
uv run python tests/agent_eval/llm_eval.py --path stream --limit 6
uv run python tests/agent_eval/llm_eval.py --path graph --limit 2 --out-stem llm_report_graph
# 报告：tests/agent_eval/report/nightly/llm_report.json（stream）与 llm_report_graph.json（graph）
# --path stream（默认）量的是产品路径：先 run_plan_context 做研究（与生成各算一个 run、
# 各一份预算，所以报告里 research_* 为 0），再逐日消费 run_generate_trip_stream 的事件；
# --path graph 量同步 /v1/generate 那张共用预算、**唯一带 validate_plans 终检**的图。
# 两条路径数值不可跨路径比较：eval_gate.py 按 PATH_EXPECTATIONS 分列期望（深度下限各算
# 各的），nightly 两条都跑，graph 报告用 `eval_gate.py --path graph` 查。
# 报告另带 prompt_sha256（两套开放生成 Prompt 正文指纹）：改串不 bump 版本号时，门禁用
# 同一函数重算源码现值比对即红——Prompt 版本治理不再只靠荣誉制。
# 当前真实 LLM 口径见 report/nightly/themed_report.md（同题双跑）；2026-08-29 的
# 非主题化旧 run 已标 report_status=stale-superseded 退役（git 历史可查），不要引用旧 run
# 的 consistency_rate 与 fallback 描述。
```

- 运行接口生成后，可通过 `X-Request-ID` 与响应头 `X-Agent-Run-ID` 关联业务面与 Agent 面（同进程同一次请求）的完整链路；`GET /api/agent/v1/runs/{run_id}` 查询该运行的脱敏 Trace。Trace 事件包含 request/run/span/parent span 关联，Registry 工具事件还包含 tool call/action ID。生成响应的 `status` 为 `success`、`degraded` 或 `failed`：开放模式生成必然标注关键事实需出发前复核（`degraded`），LLM 未配置或重试耗尽返回待研究草案，0 可交付项时 `failed`（质量报告 BLOCKED，编排层不会将其落库为成功）。`GET /api/agent/v1/metrics` 提供互斥的成功率、降级率、失败率、平均事件耗时、LLM/工具/MCP 调用数、token、重试、fallback、工具预算耗尽和 MCP 失败率。当前是进程内指标，生产多实例部署应接入 Prometheus/OpenTelemetry。
