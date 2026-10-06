-- V8：条目反馈（C3.5 反馈回路，SPEC 见 docs/PLAN-C3.5-反馈回路-预研.md）
--
-- 纪律（同 V1-V7）：一经入库不得再改；后续变更一律新增 V9__*.sql。
-- 执行链：Alembic revision `0008_item_feedback` 按序执行本文件（不复制、不改写）。
--
-- 脱敏红线：本表**不进** share 投影 / 模板投影（template_summary）/ MCP 导出 /
-- 任何公开 VO——反馈是私域质量信号，user_id 不出管理员审计面之外。
-- 软删行程/条目时反馈行不级联删（eval 需要历史），不可见靠投影隔离。

CREATE TABLE item_feedback (
    id BIGINT NOT NULL AUTO_INCREMENT,
    itinerary_id BIGINT NOT NULL COMMENT '冗余存行程 id,聚合按行程/城市走 join',
    item_id BIGINT NOT NULL COMMENT 'itinerary_item.id',
    user_id BIGINT NOT NULL COMMENT '记录实名,展示匿名',
    value TINYINT NOT NULL COMMENT '1=right 0=wrong',
    reason VARCHAR(32) NULL COMMENT 'value=0 必填:wrong_location/wrong_time/wrong_price/not_interested/closed/other',
    note VARCHAR(200) NULL COMMENT '可选备注',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_feedback_item_user (item_id, user_id),
    KEY idx_feedback_itinerary (itinerary_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='条目对/错反馈(C3.5):一人一条可改,不进任何投影';
