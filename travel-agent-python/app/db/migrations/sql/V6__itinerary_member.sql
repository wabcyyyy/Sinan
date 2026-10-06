-- V6：协作成员与邀请（SPEC C2.3）
--
-- 纪律（同 V1/V2）：一经发布不得再改；后续变更一律新增 V7__*.sql。
-- 执行链：Alembic revision `0006_collaboration` 按序执行本文件（不复制、不改写）。
--
-- 设计要点（PLAN C2.3）：
--   * 独立邀请表 + 成员表，未兑换邀请不塞进成员行；
--   * 成员关系硬删（移除后可重新邀请），不继承软删混入；
--   * 邀请链接 token 使用密码学安全随机数，库内只存 sha256 摘要；
--   * owner 不插成员行（owner 由 itinerary_main.user_id 定义）。

-- 1) 成员表：role 只准 editor/viewer；owner 永远走主表 user_id 判定
CREATE TABLE itinerary_member (
    id BIGINT NOT NULL AUTO_INCREMENT,
    itinerary_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    role VARCHAR(12) NOT NULL COMMENT 'editor/viewer；owner 不在此表',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_itinerary_member (itinerary_id, user_id),
    KEY idx_member_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2) 邀请表：单次兑换、默认 7 天有效、owner 可逐条撤销；无受邀 user_id（兑换时才定人）
CREATE TABLE itinerary_invitation (
    id BIGINT NOT NULL AUTO_INCREMENT,
    itinerary_id BIGINT NOT NULL,
    role VARCHAR(12) NOT NULL COMMENT 'editor/viewer',
    token_hash CHAR(64) NOT NULL COMMENT 'sha256 hex；明文 token 不落库',
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    accepted_at DATETIME NULL,
    accepted_by BIGINT NULL,
    revoked_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_invitation_token_hash (token_hash),
    KEY idx_invitation_itinerary (itinerary_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
