-- V10：生成参数持久化（恢复保真，SPEC 见 docs/审查-对抗性面试深挖-2026-09-26.md §4 P0-3）
--
-- 纪律（同 V1-V9）：一经入库不得再改；后续变更一律新增 V11__*.sql。
-- 执行链：Alembic revision `0010_gen_params` 按序执行本文件（不复制、不改写）。
--
-- 为什么建列：僵尸续跑的 rebuild_request 此前只从主表反推 8 个参数，
-- intent/requirements 主表根本没有列可读——续跑出来的行程丢了用户的
-- 一句话意图（生成质量静默劣化，指纹也感知不到参数劣化）。origin_city
-- 在 V9 已有列但 rebuild 没读，本次一并接上。
--
-- 脱敏红线：intent/requirements 是用户原话，属私域数据——不进 share 投影 /
-- 模板投影（template_summary）/ MCP 导出 / 任何公开 VO。

ALTER TABLE itinerary_main
    ADD COLUMN intent VARCHAR(800) NULL COMMENT '旅行意图(M1,最长800字):建壳时写入,僵尸续跑重建命令时读回',
    ADD COLUMN requirements TEXT NULL COMMENT '补充要求(自由文本):建壳时写入,僵尸续跑重建命令时读回';
