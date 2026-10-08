-- V14：规划修订号（M4 并发应用 CAS，spec 见 docs/sinan-experience-reliability-spec-20261008.md §9.2）
--
-- 纪律（同 V1-V13）：一经入库不得再改；后续变更一律新增 V15__*.sql。
-- 执行链：Alembic revision `0014_itinerary_planning_revision` 按序执行本文件。
--
-- 为什么建列：草稿 baseRevision（SHA-256 指纹）是"先读 hash 再写"，存在竞争窗口——
-- 两个并发 apply 都能读到同一指纹然后都写入。整数 planning_revision 由所有修改
-- 规范行程内容的业务写入口在同一事务内 +1，apply 以
-- `UPDATE ... SET planning_revision = planning_revision+1 WHERE id=? AND planning_revision=?`
-- 做原子条件更新，过期即 409。收藏/封面等不改变规划内容的写不推进。
--
-- 草稿兼容：部署前的存量草稿没有 _basePlanningRevision 键，apply 时退化为
-- 既有 hash 比对（不取消其保护），新草稿起走 CAS。

ALTER TABLE itinerary_main
    ADD COLUMN planning_revision BIGINT NOT NULL DEFAULT 0 COMMENT '规划修订号(M4):内容写入口事务内+1,apply 原子条件更新过期409';
