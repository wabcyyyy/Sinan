# 司南 Sinan 智能旅行规划

基于 **AI Agent（LangGraph）+ 多 Agent 研究** 的旅行规划系统。输入城市、天数、偏好与一句话旅行意图，Agent 生成 AI 规划的每日行程：关键事实如实标注来源与核验状态（外部数据命中项 `observed`、LLM 估价项 `estimated`），并附地图深链引导出发前核实；生成过程通过 SSE 实时推送进度。前端为 **React 19 + Vite + TS**（入口 `src/react-app/`）：对话式创建（首页一句话收集槽位、缺槽自动追问）、地图可视化、自然语言改行程、预算展示与 PDF 导出。后端为**单一 FastAPI 服务**（Python 3.12）：业务接口、agent 编排与事件流同进程，Spring Boot 版已退役删除（见「后端演进」）。

> **AI-NATIVE 数据面（2026-09-17）**：本地不再维护景点语料库（`poi_knowledge` 随 V4 迁移退役，也不再需要 Qdrant）——地点事实走「LLM 世界知识 + 联网搜索（DashScope enable_search）+ **OpenTripMap**（坐标/分类/图片/百科简介）+ **Nominatim**（点名→坐标兜底）」，票价/营业时间由 LLM 估价并按 `estimated` 如实标注，前端以**地图深链**（国内高德 URI / 海外谷歌 Maps）核实。底图仍为 **OpenFreeMap** 免 key 在线矢量瓦片（MapLibre，署名随图）；图片仍可走 Unsplash/Pexels/维基（服务端代理，可选、未命中落占位块）。任意城市即开即用，不再受语料覆盖城市限制。

## 核心能力

- **整段流式生成 + SSE 实时进度**：一次结构化 JSON 调用生成整趟（模型看得见全盘），逐天 yield 事件边生成边落库（生成期跨天去重 + 落库后 `validate_plans` 终检，违规天重排）；断线降级轮询，缺天自动逐日修复
- **意图优先**：一句话旅行意图作为最高优先级信号，驱动主题提炼、检索补池与选点；输出每日叙事、拍照机位、天气备选与方案分叉
- **多 Agent 研究编排**：酒店/景点/美食三域并行收集证据（LLM 规划检索 → OTM 半径池/联网搜索补池 → 评估充分性 → 不足补查），Supervisor 汇总并按校验反馈定向补查，全程 trace
- **诚实降级**：行程项带 `source` / 核验状态；外部数据与估价严格分开标注（`observed` vs `estimated`），不存在「用候选直接拼装行程冒充生成」的路径，LLM 失败如实返回待研究草案
- **对话式创建与编辑**：首页 ChatIntake 一句话收集槽位（缺槽自动追问、确认卡拍板）；详情页 ChatPanel 自然语言改行程（换酒店/调节奏/加减天数），SSE 返回草稿卡片，确认后应用并联动预算重算（服务端草稿 + 指纹乐观并发）
- **AI-NATIVE 地点层**：OTM 半径池带坐标/热度，Nominatim 点名兜底，零本地语料、零索引维护；任意城市即开即用
- **可观测**：`X-Agent-Run-ID` 关联全链路脱敏 Trace，`/v1/metrics` 暴露成功/降级/失败、token 分场景、外部调用命中（支持 Prometheus 文本）
- **前端外观契约**：七套 scheme × 日间/夜间双主题 × 紧凑密度 × 减弱动效，首帧无 FOUC；`theme:lint` 禁裸色与野 z-index、`ep:lint` 钉死 Element Plus 零用量防回潮（均入 CI）

## 系统架构

```
┌─────────────────────┐              ┌────────────────────────────────────────────┐
│ travel-frontend-react │     HTTP     │            travel-agent-python             │
│ React 19 + Vite+TS  │ ───────────► │   FastAPI 单进程后端（业务 + Agent）         │
│ 地图/预算看板/编辑    │     /api     │  ┌──────────────────┐  ┌─────────────────┐ │
└─────────────────────┘              │  │ 业务层            │  │ Agent 层        │ │
                                     │  │ JWT/行程状态机/    │  │ LangGraph 编排/  │ │
                                     │  │ 缓存/异步 PDF/SSE  │  │ 多 Agent 研究/   │ │
                                     │  └────────┬─────────┘  │ 事实落地/places  │ │
                                     │           │ 同进程直调  └────────┬────────┘ │
                                     └───────────┼───────────────────────┼─────────┘
                                                 │                       │
                                          ┌──────▼───────┐   ┌───────────▼──────────────────────┐
                                          │ MySQL 8      │   │  外部地点层（app/agent/data/places.py） │
                                          │ 用户数据      │   │ OpenTripMap 坐标/分类/图片        │
                                          │ +城市字典     │   │ Nominatim 点名→坐标（1rps 兜底）  │
                                          │ +消费基准     │   │ 联网搜索补店名 → LLM 估价/深链核实 │
                                          └──────┬───────┘   └──────────────────────────────────┘
                                                 │
                                          ┌──────▼───────┐
                                          │ Redis        │ 缓存/限速/日锁/会话吊销（事件主链路在进程内，Redis 仅尽力广播）
                                          └──────────────┘
```

**分层**：前端只做展示与交互；FastAPI 服务同时承担两件事——**业务面**（认证、行程状态机、缓存、异步 PDF 导出、SSE 事件流）与 **Agent 面**（LLM 编排、多 Agent 研究、事实落地与质量校验），两者同进程直调，省掉跨语言序列化与一条 SSE 转发桥。外部数据永远只作证据：候选以编号参考资料注入 Prompt 引导模型选点，OTM/Nominatim 命中项落地真实坐标与来源（`observed`）；票价/营业时间无结构化来源，由 LLM 估价（`estimated`）；系统不存在「用候选直接拼装行程」的路径，LLM 失败时如实返回待研究草案。

**资源口径（L5a，2026-09-27 实测）**：「一次 N 天生成花多少调用/token/秒」按 run_id 聚合可答（`travel-agent-python/scripts/usage_report.py`）。本机实测：2 天行程主生成 = 8 次 LLM 调用 / 18,223 token / 274 秒；对话式创建的每轮澄清 ≈ 2,700 token / 3-8 秒。离线评测为 mock 口径、结构性无 LLM 成本（探针实证 calls=0），故成本棘轮挂在 nightly 真实评测之后（`usage_report.py --check`，LLM 方差超限按基线认账）；线上时点数据经 `/api/agent/v1/usage` 查询。

**后端演进（Java 版已删除）**：这个项目最初是「Vue + Spring Boot + Python Agent」三端结构，Spring 侧承载认证 / 行程状态机 / 缓存 / PDF / SSE 网关。为了把 agent 能力与持久化放进同一进程，后端已按绞杀者路线整体迁到 FastAPI：当前端点口径以**可对账数字**为准——**业务面+agent 面合计 104 paths / 120 ops（2026-10-08 自 contracts/openapi.json 实数），由 `contracts/openapi.json` 入仓并受契约漂移门禁保护；前端调用点 100% 路由覆盖有机检**（`tests/test_cutover_contract.py`）。Java 时期「50 个端点落地 49」的说法只有历史出处（ARCHIVED.md），口径已随 Java 删除不可现场对账，不再作为等价性主张；恢复步骤与「原先谁负责什么」的对照表见 `ARCHIVED.md`。

## 快速开始

### 前置条件

- Docker（MySQL 8 + Redis）或本地同版本服务——**Docker 只承担这两件基础设施**：语料库退役后向量库（Qdrant）已从编排中移除
- Node.js 18+、Python 3.12 + [uv](https://docs.astral.sh/uv/)
- LLM API Key（阿里云百炼等 OpenAI 兼容端点）；建议再注册一个 **OpenTripMap 免费 key**（opentripmap.io，景点坐标/分类/图片；不配也能跑，景点池降级为纯联网搜索）。不需要任何地图平台 key

### 1. 初始化数据库与基础设施

```bash
docker compose up -d mysql redis
# 表结构无需手工执行 SQL：后端启动时自动迁移（Alembic 按版本序执行 app/db/migrations/sql/ 下的 V*.sql；
# 空库全量建表，已有表的库只打版本点、绝不重跑 DDL；V4 已退役 poi_knowledge/hotel_room_type）
# 可选种子：城市消费基准 mysql -uroot -p travel_assistant < sql/seed_data.sql
#（city_geo 城市字典的种子内置于 V2 迁移，无需手工导入）
```

### 2. 启动后端（travel-agent-python，业务 + Agent 同一进程）

```bash
cd travel-agent-python
cp .env.example .env        # 必填 DB_PASSWORD、JWT_SECRET（≥32 位）、AGENT_INTERNAL_TOKEN、LLM_API_KEY；建议填 OTM_API_KEY（免费）
uv sync
uv run python main.py       # 127.0.0.1:8000；启动即迁移 schema（openapi.json 由 scripts/export_contracts.py 单点导出）
```

### 3. 启动前端（travel-frontend-react）

```bash
cd travel-frontend-react
cp .env.example .env        # 空白模板：前端不需要任何 key
npm install
npm run dev                 # http://localhost:5173（/api 代理到 :8000）
```

> Windows 也可用根目录 `start-all.cmd` 一键拉起全部进程（`stop-all.cmd` 全停）。
> 未配置 `LLM_API_KEY` 时 Agent 如实返回待研究草案（不会用候选拼装行程冒充结果）。

### 4.（可选）容器化整栈（apps profile）

不想装 Python/Node 也可以全容器运行（首次构建较慢，之后有缓存）：

```bash
cp .env.compose.example .env    # 根目录：MySQL 口令/库名（应用密钥仍放 backend .env）
cd travel-agent-python && cp .env.example .env   # 必填项同上（JWT_SECRET 等）
cd ..
docker compose --profile apps up -d --build    # mysql+redis+agent-python+frontend
# 宿主机 3306 已被本地 MySQL 占用时：MYSQL_PORT=3307 docker compose --profile apps up -d --build
# 容器内 DB/Redis 连接由 compose 注入（DB_HOST=mysql、REDIS_URL=redis://redis:6379/0），
# backend .env 里的本地开发连接串不会影响容器
```

打开 http://localhost:5173 即可注册使用（后端 :8000 承载 /api）。升级 = `git pull` 后重跑上面最后一条命令（镜像重建 + 启动即迁移，见「运维」）。

> 对外部署（单 VPS + 域名 + 自动 HTTPS）走 prod profile + Caddy 边缘，链路不同：
> 见 [`deploy/README.md`](deploy/README.md)，一条 `bash deploy/deploy.sh` 完成构建、拉起与冒烟门。

### 干净机器 5 分钟验证单（勾选式）

- [ ] ① `docker compose up -d mysql redis`（或直接走第 4 步整栈）
- [ ] ② `cd travel-agent-python && cp .env.example .env`，填 DB_PASSWORD、JWT_SECRET（≥32 位）、AGENT_INTERNAL_TOKEN、LLM_API_KEY
- [ ] ③ `uv sync && uv run python main.py` 起后端（启动即自动迁移 schema）
- [ ] ④ `cd travel-frontend-react && npm install && npm run dev` 起前端
- [ ] ⑤ 打开 http://localhost:5173 → 注册并登录
- [ ] ⑥ 输入城市/天数/一句话意图 → 生成行程（无 LLM key 时如实返回待研究草案）
- [ ] ⑦ 点开任意行程项的地图深链核实（国内高德 / 海外谷歌；餐饮/酒店可能无坐标，按名称搜索）
- [ ] ⑧ `curl http://localhost:8000/api/agent/health` 返回 up

### 5.（可选）查看迁移前的 Java 实现

`travel-backend-java/` 是迁移前的第一版后端，**已从工作树删除**。需要对照时按 `ARCHIVED.md` 从 git 历史取回（含"原先谁负责什么"的 Spring → FastAPI 位置对照表）。

## 运维（自托管）

> 所有备份/恢复操作前**先停掉全部写入方**（agent-python、前端用户流量、后台任务），MySQL 保持运行；工具本身不启停服务。

### 备份 / 恢复

```bash
# 备份（SQL dump + 后端 data/ 目录打成一份 bundle；输出目录的父目录须已存在）
just backup '--out "D:/backups/travel-20260917"'

# 恢复（要求目标库已存在且为空、目标 data 目录不存在、显式 --yes；SQL 导入非事务，失败不自动回滚）
just restore '--from "D:/backups/travel-20260917" --yes'
```

- bundle 内容：`content/dump.sql`（单事务一致性快照）+ `content/data/`（上传图与导出文件完整拷贝）
- 口令只在容器内展开（`MYSQL_PWD`），不进宿主机命令行参数；已存在的输出/数据目录一律拒绝覆盖
- 隔离演练：加 `--compose-file/--project-name/--data-dir` 指向独立的 compose 工程与目录，不碰生产实例
- bundle 含用户数据与口令哈希，妥善保管、勿入版本库；Redis（缓存/会话吊销）不在备份范围，恢复后旧会话自然失效需重新登录

### 升级

```bash
git pull
docker compose --profile apps up -d --build   # 镜像重建；容器启动即按版本序执行迁移（Alembic）
bash deploy/deploy.sh                          # 生产（prod profile + Caddy）：拉码→重建→冒烟门
```

- 迁移 append-only：旧库（含已退役的 `poi_knowledge`）启动时只打点/增量执行，绝不重跑历史 DDL；演练已验证 V3 旧库 → V4 平滑升级，数据与写入不受影响
- 宿主机路径部署时：`git pull` 后重启后端进程即可（`uv run python main.py` 启动即迁移）

### 升级演练记录（2026-09-17，一次性容器实测）

- 造库：用仓库自身迁移链 V1→V2→V3 建库 + 种入 `poi_knowledge`/`hotel_room_type` 与 legacy 用户，`alembic_version=0003_addons`
- 迁移：项目真实迁移入口 `ensure_schema()` 执行 `0003_addons → 0004_retire_poi_knowledge`
- 验证：两张退役表已删除、legacy 用户数据完好、迁移后行程表写入成功
- 备份→恢复演练：对运行中实例实测 backup（126MB bundle），dump 导入一次性 MySQL 8 容器后行数/版本点/结构完整

## 测试与评测

```bash
just check                       # 一键门禁（九步：lint/format/typecheck/边界/规模/密钥/测试+覆盖率/diff-cover/契约漂移）
just test                        # 后端离线单测（零外部依赖，全走离线替身）
just eval                        # Agent 离线评测（mock 驱动，行为改动后对比指标）
just fe-check                    # 前端构建 + theme:lint + ep:lint + 单测
```

| 套件 | 说明 |
| --- | --- |
| 离线单测 | 事件契约与漂移门禁 / 断流取消 / 叙事契约 / 引用落地 / 跨天去重 / 反思 / 多 Agent / SSE / PDF 导出 / 后台管理 |
| 活栈契约 | `uv run pytest tests/api -q`（需先起后端）：登录/行程/导出/后台端到端 |
| Agent 评测 | 离线 mock 评测 + 真实 LLM 评测（固定模型与 Prompt 版本）；行为回归门禁（快照 + eval ratchet）已入 CI |
| 性能压测 | `uv run python tests/perf/load_test.py --endpoint all` |

> 完整命令清单与门禁口径见 [`AGENTS.md`](AGENTS.md) 与 [`travel-agent-python/AGENTS.md`](travel-agent-python/AGENTS.md)。

## 仓库结构

```
Travel-Assistant/
├── AGENTS.md               # 跨仓约定与哲学（命令、契约流程、门禁纪律）— 改代码前先读
├── ARCHIVED.md             # Java 版退役说明：位置对照表 + 从 git 历史取回的命令
├── docker-compose.yml      # 本地基础设施（MySQL + Redis；可选 apps profile 容器化前后端）
├── justfile                # 单命令入口（just check / test / eval / snapshot / fe-check）
├── travel-agent-python/    # FastAPI 单进程后端：业务接口 + LangGraph 编排 + 多 Agent 研究 + 外部地点层（见其 AGENTS.md）
│   └── app/agent/README.md # 生成链路模块蓝图（依赖方向与"新增能力先抄谁"）
├── travel-frontend-react/  # React 19 + Vite + TS（入口 src/react-app/）：对话式创建首页 / 详情页对话编排·地图 / 探索页；src/shared/ 框架无关纯函数
├── contracts/              # 跨端契约导出物（schema + openapi + 前端生成类型，drift 门禁）
└── sql/                    # 城市级消费基准种子（语料采集管线已随 V4 退役，见 git 历史）
```

## 安全说明

- 真实密钥只存 `.env`（已 gitignore），仓库仅跟踪 `.env.example`；**公开 / 部署前必须 rotate** 全部第三方 Key 与本地 `JWT_SECRET`、`AGENT_INTERNAL_TOKEN`、MySQL 口令
- 后端启动强制校验 `JWT_SECRET`（非空且 ≥32 字符；规则沿用迁移前 Spring 侧的 `@NotBlank @Size(min=32)`）；`AGENT_INTERNAL_TOKEN` 保护 HTTP 直调 agent 的入口，服务绑定非回环地址且未配置 token 时启动 fail-fast
- **会话凭据**为 HttpOnly Cookie（`SameSite=Lax`），登录响应不回传 JWT；支持服务端吊销（`/api/auth/logout` 将 token 写入 Redis 黑名单）与多端登出；写请求校验 Origin 白名单（CSRF 缓解）
- 登录 / 注册按「IP + 用户名」滑动窗口限速，限速 / 日锁 / 续跑标记统一走 Redis，故障自动降级进程内
- 生成 / 编辑类接口的错误响应只回通用文案，内部异常原文完整记日志不外泄；Trace 落盘前对 URL key 与 Bearer 令牌集中脱敏
- 第三方合规：OpenTripMap 数据源为 ODbL（OpenStreetMap/Wikidata 衍生，允许缓存，署名见底图 attribution）；Nominatim 走公共实例时 UA 含联系方式且客户端自节流至 1 rps；本项目定位**本地演示 / 学习项目**，未做生产级合规审计

## 已知局限

| 局限 | 说明 |
| --- | --- |
| 非生产系统 | 单机演示；无集群、无真正多实例限流 / 会话吊销集群方案 |
| 价格与营业时间 | 无结构化事实来源：票价/营业时间由 LLM 估价（`estimated`），**不是实时成交价、不保证仍营业**——出发前用行程页地图深链核实 |
| 餐饮/酒店坐标 | OpenTripMap 对餐饮/住宿基本无覆盖，此类点位可能无坐标（地图不画点、深链按名称搜索，如实缺省） |
| OTM 语言 | OpenTripMap 仅 en/ru，外部候选名为英文；中文名由 LLM 对齐，个别小众点位名称可能以英文呈现 |
| 海外坐标 | 景点坐标来自 OTM/Nominatim，解析失败的城市如实留空（界面提示，不假装有数据） |
| 多 Agent 定性 | 酒店/景点/美食三域**并行工具研究** + Supervisor 汇总补查，不是自主协商的多 Agent 协作 |
| 记忆边界 | 请求内 WorkingMemory + 对话滑窗（4 轮 × 800 字）；无长期画像、无跨会话向量记忆 |
| 评测口径 | 离线单测与真实 LLM 评测分开报告；小样本评测结果不能外推全量（检索质量评测已随语料库退役移除） |
| 编辑链新增项 | 对话式编辑加入的点位**直接落库、不过接地链**：新项零坐标零标签（界面已提示"尚未核实、可能无坐标"），跨天去重也不覆盖编辑链 |
| 备选池标注 | 备选池是轻量形态：只有带坐标的建议才带"未核实"提示；估算花费按 `estimatedCost` 如实标注但非观测价 |
| 流式终检粒度 | 整段流式落库后跑 `validate_plans` 终检、违规天重置重排；但终检只在**落库后**逐天判定，不含跨天路线矩阵（路线矩阵仅 trip 图分支） |
| 单进程前提 | 幂等原子性、SSE 事件总线、活跃生成存活探测、agent 面限流四处都按**单进程**实现；多实例部署需先升级为 DB 租约 / Redis 共享 |
| 时区前提 | 僵尸恢复的 5 分钟活跃窗口要求应用与 MySQL 同时区；启动后由 `timezone-check` 周期任务对照 `@@session.time_zone` 记 warning，**只警告不阻断** |
