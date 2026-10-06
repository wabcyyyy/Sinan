# Travel Assistant Badcase 回放记录

这些案例是离线故障注入/契约回放，不是线上事故。每次回放固定输入和规则版本，
用于防止“模型看起来生成成功”掩盖业务不可用。

## A：真实交通耗时导致营业时间越界

- 输入：相邻 POI 之间只留 30 分钟，注入路线服务返回 180 分钟。
- 预期：规则校验报告路线时间不足；优化器调整顺序或移除不可执行点位。
- 首次偏离：`reflect.validate_plans` 的路线间隔检查。
- 根因：坐标距离估算无法表达跨区域/道路绕行。
- 修复：`RouteService` 提供真实路线适配层，`Schedule Optimizer` 将交通时间纳入时间轴，并保留 `source`/`degraded`。
- 回放：`tests/agent_eval/replay.py` 的 `route` 案例；路线/优化器对照见 `report/offline/baseline_report.md`。

## B：异步生成响应丢失导致重复落库

- 输入：同一日期的 Agent 结果已经成功写入，但调用方没有收到响应，随后使用相同动作 ID 重试。
- 预期：最终只有一份日期结果，重试只读取 `SUCCEEDED` 状态，不重复插入行程项。
- 首次偏离：旧实现没有稳定的日期动作标识和写入前状态判断。
- 根因：请求状态和业务写入状态没有绑定，超时被误判为“未执行”。
- 修复：Java `ItineraryDayPersistenceService` 使用 `day-{itineraryId}-{dayNo}`、参数指纹和 `PENDING/RUNNING/SUCCEEDED/FAILED` 状态；成功状态在写入前直接返回。
- 验收：重复调用 `persist` 时检查 `SUCCEEDED` 短路；数据库集成环境还应验证唯一约束、事务回滚和服务重启恢复。

## C：自然语言编辑越权修改未要求部分

- 输入：用户只要求删除一个景点。
- 预期：只删除目标项，其他日期、酒店和预算口径保持不变。
- 首次偏离：开放式计划重写可能返回整份被改动的计划。
- 修复：对话草稿使用 `baseRevision`，Java 应用层校验 POI 身份、字段白名单和版本一致性；计划应用前后建立版本快照。
- 回放：现有对话编辑/草稿应用回归测试；版本 Diff API 可用于核对实际变更范围。
