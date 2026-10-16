CREATE TABLE trial_invite (
    code_hash VARCHAR(64) NOT NULL PRIMARY KEY,
    created_by VARCHAR(64) NOT NULL,
    consumed_by BIGINT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    used_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE trial_wallet (
    user_id BIGINT NOT NULL PRIMARY KEY,
    balance_micros BIGINT NOT NULL,
    spent_micros BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE trial_ledger (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    kind VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    reserved_micros BIGINT NOT NULL DEFAULT 0,
    amount_micros BIGINT NOT NULL DEFAULT 0,
    model VARCHAR(128) NULL,
    detail JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_trial_ledger_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
