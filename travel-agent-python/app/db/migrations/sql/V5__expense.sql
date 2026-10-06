-- V5：记账表（SPEC C2.1）。软删语义与 ItineraryItem 对齐（deleted 标记，查询全局过滤）。
CREATE TABLE expense (
    id BIGINT NOT NULL AUTO_INCREMENT,
    itinerary_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    category VARCHAR(16) NOT NULL COMMENT 'attraction/food/hotel/transport/shopping/other',
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(8) NOT NULL DEFAULT 'CNY',
    day_no INT NULL,
    item_id BIGINT NULL COMMENT '关联行程项,可空',
    spent_at DATE NULL,
    payment_method VARCHAR(24) NULL,
    note VARCHAR(255) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted TINYINT NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_itinerary (itinerary_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
