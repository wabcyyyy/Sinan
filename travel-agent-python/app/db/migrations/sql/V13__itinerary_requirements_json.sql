-- V13：结构化需求持久化（M1a 需求单一真源，spec 见 docs/sinan-experience-reliability-spec-20261008.md §5.3）
--
-- 纪律（同 V1-V12）：一经入库不得再改；后续变更一律新增 V14__*.sql。
-- 执行链：Alembic revision `0013_itinerary_requirements_json` 按序执行本文件（不复制、不改写）。
--
-- 为什么建列：TripRequirements 的结构化语义（日窗口/必去/排除/节奏/交通/预算口径/
-- 住宿/未决请求）此前无处可存，恢复与重生成只能靠自然语言 requirements 原话反推。
-- 建壳时写入规范化后的需求 JSON，generation_recovery.rebuild_request 读回同一结构。
--
-- NULL 语义 = 旧行程（本迁移前创建）：按旧默认规则读回，不反推用户没说过的约束；
-- 新行程建壳一律写入（全默认需求也写 '{}'），作为新旧指纹口径的判定信号。
--
-- 脱敏红线：同 intent/requirements（V10）——用户原话衍生的私域数据，不进
-- share 投影 / 模板投影（template_summary）/ MCP 导出 / 任何公开 VO。

ALTER TABLE itinerary_main
    ADD COLUMN requirements_json JSON NULL COMMENT '结构化需求快照(M1a):建壳写入规范化TripRequirements,恢复读回;NULL=旧行程按旧默认规则';
