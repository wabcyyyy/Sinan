-- V9：往返航班报价（L14 航班链路，SPEC 见 docs/PLAN-真实数据面-2026-09-25.md）
--
-- 纪律（同 V1-V8）：一经入库不得再改；后续变更一律新增 V10__*.sql。
-- 执行链：Alembic revision `0009_flight_quotes` 按序执行本文件（不复制、不改写）。
--
-- 存 JSON 而不是建报价表：报价是**整趟行程级**的少量行（≤3 条往返），随行程
-- 生成一次写入、随行程读取，没有独立查询/聚合需求（反冗余，哲学 5）。观测时点
-- 与 provider 随行落在 JSON 内（FlightQuote.retrieved_at/expires_at/provider）。
--
-- 脱敏红线：报价含**用户出发地**（origin_city），属私域数据——不进 share 投影 /
-- 模板投影（template_summary）/ MCP 导出 / 任何公开 VO，只在 owner 与协作者的
-- 详情面出现（与 itinerary_main 其余列的可见性一致）。
--
-- 可见性例外说明：本列存的是真实观测价，前端据此渲染 observed 徽章；空/NULL
-- 一律表示"这次没有观测到报价"，绝不回填旧值（见 day_persistence.save_flight_quotes）。

ALTER TABLE itinerary_main
    ADD COLUMN flight_quotes JSON NULL COMMENT '往返航班聚合报价(FlightQuote 数组,L14):观测事实,空即本轮未观测到,不回填旧值',
    ADD COLUMN origin_city VARCHAR(64) NULL COMMENT '出发地城市名(L14):用户建行程时填,查航班/实时价的唯一来源';
