# 上线 v1 加固轮证据（2026-10-16）

本轮是 `docs/launch-v1/` 计划 T1–T9 的一次集中执行。报告口径与 `docs/evidence/README.md` 一致：
**只写已发生的事实与实测结果**，未跑的就不写成跑过。证据包：`launch-v1-hardening-2026-10-16/`。

候选 SHA：**`dcf393a`**（`master`，已推送 origin）。本轮提交链：

| SHA | 任务 | 内容 |
| --- | --- | --- |
| `e0b4c94` | T0 | 上一轮基线（本轮起点，非本轮产物） |
| `4d83996` `e22a5c0` | T1 | 持久执行面 V16 `planning_task`（状态/租约/绝对截止/attempt/累计账本）+ 取消端点 |
| `0f153ad` | T1 | 五项补洞：活租约不可抢占、取消 × 持有者已死可收敛、已结算行走新 attempt、被拒写入不再丢账、日级写入受所有权约束 |
| `0b1b048` | T4 | 取消链路产品可达（前端）+ 取消后终态如实文案 |
| `fc203a2` `2d1916f` `dcf393a` | T8a | 生产发布入口（compose/Caddy/deploy.sh/provision.sql/OCI SHA 标签）+ 真起栈修掉的 3 缺陷 + runbook |
| `d586f56` | T5 | T5b 检索类别不得覆盖用途（饮食闸被绕的成因）、T5a 排程前资格筛选、T5d 结构化抽取 + 有界修复 |
| `03ba725` | T2 | V17 `cost_ledger` + `llm_reservation`、`cost_policy` 纯决策层、四层窗口、BYOK/platform 归属分离 |
| `6cad4d3` | T2 | **启动接线修复**：`install_backend()` 此前无人调用 |
| `1243c04` | T4 | 终检抛错不再被当成通过（PARTIAL + 最终预算 + 稳定码） |

## 实测门禁（原始输出见 `gates-dcf393a.log`）

**通过**
- `verify.ps1 -NoCache`：合树（35 改动文件 / 6 选中测试文件）passed，收据副本 `verify-combined-tree.json`；
  T1 第二轮收据副本 `verify-t1-second-round.json`（pytest 步 79.0s < 90s 预算）。
- 离线四套评测 + 逐指标棘轮：`success 0.9231 / degraded 0.0769 / failed 0.0 / budget_deviation 0.0 / category_reasonable 1.0`，
  `eval ratchet OK: 31 项指标在基线内`。**本轮没有任何一项基线被放宽或改动。**
- `lint-imports`：Contracts 4 kept / 0 broken。`ruff check app tests`、`ruff format --check`：干净。
- 前端四门：`npm run build`、`test:unit`（含 2 例新增取消回归）、`theme:lint`、`ep:lint`：全部通过。
- 隔离 MySQL 8.0.46 smoke（一次性容器、空库按 `sort -V` 灌 `V*.sql`）：20/20，凭据与容器已销毁（T1 首片）。

**未通过 / 未知**
- `code_metrics.py`：**FAIL** `long_functions 34 > 33`、`huge_files 16 > 11`。越线的新增文件：
  `models.py 578`、`planning_tasks.py 496`、`cost_ledger.py 460`、`planning_task_store.py 449`、`config.py 421`。
- `scripts/typecheck.py`：FAIL（INV-1 只减不增）；其中 2 条在本轮测试文件内修掉，**全库 0 errors 未重新确认**。
- CI：run `38835557115` / `38836121180` / `38837204371` / `38837437328` **全部 failure**，耗时 11–36s，
  失败点均为 preflight 的 code metrics 阶段（见 `ci-runs.txt`）。
  **关键事实：CI 卡在第一阶段，因此其后的全量 pytest、`diff-cover ≥70%`、契约漂移、stream golden、
  compose config、preview-stack、前端 job 在本轮全部提交上从未真正执行过，结果未知。**

## 本轮测出来的两条真实缺陷（都不是靠绿灯发现的）

1. **成本账本没接上服务进程。** `cost_ledger.install_backend()` 的 docstring 写着"`main.py` lifespan 调用"，
   但代码中无调用点（`git grep cost_admission`/调用点核查）。一次有界真实样本的日志把它暴露出来：
   `no durable cost backend installed; 1 cost event(s) stayed in-memory only (expected for offline scripts/tests, NOT for the served app)`。
   影响：用户/行程/平台硬顶在服务进程里读到的是空账——费用控制"实现了但一分不记"。修复 `6cad4d3`。
2. **题集里没有严格饮食用例。** `tests/agent_eval/cases.json` 共 13 例，按 `素食/忌口/清真/无麸质/diet/vegan` 检索零命中。
   因此本轮 `llm_eval.py --path stream --limit 3`（先报价、≤3 行程、沿用 32 调用/80k token 上限、失败不重试、退出码 0）
   **无法**推进"严格饮食模式 0/5 真实样本"这条 P1；该修复目前只有离线确定性证据。

## 未执行 / 未验证（不得计为通过）

- 真实 SIGKILL 整进程重启与端口占用演练（本轮用清句柄 + 拨老租约 + 换 worker 身份，在 SQLite 与真 MySQL 上各演一遍）。
- `tests/api` 活栈契约套件（需真 MySQL + Redis + LLM key）。
- T2 验收场景中的并发不超卖、Redis 降级/切回、供应商不回 usage、崩溃未结算有界回收：实现方自述，集成侧未复核；
  `sweep_expired_reservations` 尚未被 cron 登记（`main.py` 只登记 usage/checkpoint 清理）。
- 浏览器关键路径（注册登录、ChatIntake、SSE 断线重连、地图深链、对话提案确认、补齐、分享撤销、PDF、移动端 390px、弱网刷新）：未跑。
- 容量拐点（模型桩 1/2/4/8 并发的准入/排队/写入/取消）：未跑。
- 备份恢复演练、告警实际送达、真实域名与证书、镜像推送：无凭据/无配置，未跑。
- 外部依赖实况：OpenTripMap 本轮连续 SSL `UNEXPECTED_EOF` 失败并触发熔断降级；
  搜索角色提示 `api.deepseek.com` 无 `enable_search` 能力，需 `LLM_ROLE_SEARCH` + dashscope 配置才能真正启用。

## 结论

**NOT READY（不可向真实用户公开上线）。** 判据是上面那组未通过与未执行，而不是主观余量：
门禁红在体量棘轮、CI 后续门未知、"交付可信"只落了终检语义一条、账号数据生命周期与运维闭环（T8b）未做、
发布质量的头号 P1（严格饮食）仍无真实样本。逐条限制与最小补走路径见 `docs/launch-v1/handoffs/T9.md`。
