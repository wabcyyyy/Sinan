# 独立规划 worker 与故障恢复

整趟规划和自动恢复由 `worker.py` 执行，API 提交后立即返回行程 ID。两者复用同一套 Python 业务代码和 `plan_days`，MySQL 是任务、交付和累计预算的权威；没有引入消息队列。
单日重生成、聊天编辑、导出仍沿用现有业务入口。本轮不宣称所有模型调用都已移出 API。

## 本地启动

先按 [开发指南](development.md) 配置环境并启动 MySQL/Redis，再开三个终端：

```bash
# API：先完成启动迁移，再启动 worker
cd travel-agent-python
uv run python main.py
```

```bash
cd travel-agent-python
uv run python worker.py
```

```bash
cd travel-frontend-react
npm run dev
```

`just dev`、`just worker`、`just dev-fe` 对应上述入口。完整本地 Compose 的 `--profile apps up` 同时启动 API、worker 和前端；只启动 API 时，已接受的规划保留在数据库中等待 worker，不会在 API 中偷偷执行。
API 与 worker 必须使用相同数据库、模型路由、加密配置和时区。此阶段以同一宿主机为范围，两个进程共用 `data/`（Compose 已挂同一路径）；LangGraph 检查点仍是本地 SQLite。worker 不运行 DDL，启动前应先由 API 完成迁移。

## 执行与恢复规则

1. 行程主表、日期和 `planning_task.request_json` 在同一事务提交；登记任务失败时全部回滚。
2. API 登记 `QUEUED`，不申请执行租约。全局容量默认 24（含执行和排队），检查与入队由数据库锁串行化；同用户任务检查也使用数据库行锁。
3. worker 默认最多处理 4 个任务，只领取当前能执行的工作。MySQL `FOR UPDATE SKIP LOCKED` 支持多个 worker 竞争领取，执行模型时不持领取事务。
4. 领取后复用租约、心跳、取消、绝对截止、累计消耗与旧执行者保护。排队时间计入原截止期；接管不重开时间窗或调用额度。
5. 恢复每 10 秒检查一次。持久任务以租约过期为依据，不再额外等待行程闲置 5 分钟；已成功日期保留。默认租约 90 秒，实际接管时间还受扫描、可用执行槽及剩余截止期影响。
6. 过期任务停止后续模型调用并收敛为失败或部分交付；取消任务不自动复活。

`PLANNING_WORKER_CONCURRENCY` 可在 1–4 内调整，`PLANNING_QUEUE_CAPACITY` 默认 24，`PLANNING_POLL_SECONDS` 默认 1。增加进程会增加模型请求、数据库连接和外部服务调用，本轮没有量测全局吞吐上限。
worker 收到停止信号后停止领取并等待已有任务；容器宽限 330 秒，超过宽限由容器终止，剩余责任通过租约恢复。API 的 `/healthz` 不代表 worker 正常；应另外查看 worker 日志、QUEUED 堆积和租约更新。

## 跨实例 SSE

业务生成事件写入 MySQL `generation_event`，同一行程在行锁内分配序号并提交，避免并发发布时游标跳过尚未提交的较早事件。API 每 0.5 秒读取新事件，客户端持 `Last-Event-ID` 可连接任一 API 实例。
每个行程最多保留 256 帧，记录保留 2 小时；日志缺口发 `resync`，终态仍从业务库补帧。聊天私有流不进入共享回放。连接数限制仍按 API 进程计数，本轮没有实现全局连接配额。
这是小规模本地运行的实现，增加了数据库读写；没有用吞吐或延迟改善来包装这个取舍。

## 可复现故障验收

在后端目录运行：

```bash
uv run python scripts/verify_planning_worker.py
```

脚本使用当前 MySQL 连接创建随机 `sinan_worker_probe_*` 临时库，只对该库执行迁移，退出时删除该临时库。账号须有建库和删库权限；不要给线上应用账号增加该权限。日志与检查点放入临时目录。
模型输出使用 fixture，禁止外部 HTTP；费用计数是合成输入，不是供应商账单。验收使用真实 HTTP API、独立 worker 进程、MySQL 事务和操作系统进程终止，覆盖：

- API 已接受任务后退出，worker 仍能交付。
- 第一天提交后终止 worker，两个 worker 竞争接管，已交付条目 ID 保持不变。
- 接管继承截止期与累计消耗，旧令牌条件更新被拒，只产生一次最终交付事件。
- 从另一 API 实例带游标补发，事件不重复、按序且能够结束。

验收将租约缩短到 3 秒、心跳 1 秒，保留 10 秒扫描间隔，加快故障实验。测得的恢复时间仅对应这组配置，不能当作默认 90 秒租约的恢复承诺。
2026-10-22 Windows 本机 MySQL 实验中，上述七项检查通过；从终止 worker 到恢复交付观测为 3.953 秒。新 worker 启动时会立即检查一次，之后按 10 秒扫描。这是一轮固定输出的功能验收，未做恢复时延分布或吞吐测量；CI 会在 Linux/MySQL 上执行同一脚本。
这不证明真实模型质量、供应商计费恰好一次、跨宿主机高可用、数据库故障容错或高并发容量。公开固定演示仍不连接真实后端。
