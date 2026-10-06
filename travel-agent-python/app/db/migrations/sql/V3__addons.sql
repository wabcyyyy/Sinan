-- V3：运行时功能开关（Addon 体系，G-3.1）
--
-- 纪律（同 V1/V2）：一经发布不得再改；后续变更一律新增 V4__*.sql。
-- 执行链：Alembic revision `0003_addons` 按序执行本文件（不复制、不改写）。
--
-- 设计要点：
-- * addon_state **不预置行**：无行 = 走 env 默认值（WEB_SEARCH_ENABLED 等，
--   INV-6 env 键仍是默认值来源）；管理员第一次切换才落行。
-- * addon_audit 是 append-only 审计：每次切换插一行，不更新不删除。
-- * 进程内缓存（addons.py，TTL 兜底）由切换方就地失效；TTL 只为多进程部署兜底。

CREATE TABLE addon_state (
  addon_key  VARCHAR(48)  NOT NULL COMMENT 'web_search|live_price|schedule_optimizer|route_service',
  enabled    TINYINT(1)   NOT NULL COMMENT '1=启用 0=停用',
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64)  NOT NULL COMMENT '操作人 username',
  PRIMARY KEY (addon_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='运行时功能开关状态（无行=env 默认）';

CREATE TABLE addon_audit (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  addon_key  VARCHAR(48)  NOT NULL,
  enabled    TINYINT(1)   NOT NULL,
  changed_by VARCHAR(64)  NOT NULL,
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_addon_audit_key_time (addon_key, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='功能开关切换审计（append-only）';
