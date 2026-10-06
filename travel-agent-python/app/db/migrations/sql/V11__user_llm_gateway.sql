-- V11：用户自带 LLM 网关（BYOK，统一 AI 接入网关）
--
-- 纪律（同 V1-V10）：一经入库不得再改；后续变更一律新增 V12__*.sql。
-- 执行链：Alembic revision `0011_user_llm_gateway` 按序执行本文件（不复制、不改写）。
--
-- 单用户启用互斥：MySQL 无法简洁表达部分唯一（enabled=1 的 UNIQUE），由 service 层
-- 在同一事务内先 UPDATE enabled=0 WHERE user_id 再置 1 保证至多一条 enabled=1；
-- idx_gateway_user_enabled 即为这条互斥 UPDATE 的查询路径。
--
-- 脱敏红线：api_key_cipher 是 Fernet 密文，api_key_hint 只存尾 4 位——两者都**绝不**
-- 进任何 VO 明文回显 / 日志 / 异常信息 / share 投影 / 模板投影（template_summary）/
-- MCP 导出（同 V8 惯例）。明文 api_key 只允许在上游请求头构造的内存瞬间出现。

CREATE TABLE user_llm_gateway (
    id BIGINT NOT NULL AUTO_INCREMENT,
    user_id BIGINT NOT NULL COMMENT '属主;跨用户读写一律404',
    name VARCHAR(64) NOT NULL COMMENT '配置名,UNIQUE(user_id,name)',
    base_url VARCHAR(512) NOT NULL COMMENT 'OpenAI兼容网关地址',
    api_key_cipher VARCHAR(512) NOT NULL COMMENT 'Fernet密文,绝不进任何VO/日志',
    api_key_hint VARCHAR(16) NULL COMMENT '尾4位提示,如 ***abcd',
    model VARCHAR(128) NOT NULL COMMENT '该网关使用的模型名',
    enabled TINYINT NOT NULL DEFAULT 0 COMMENT '单用户至多一条1,由service层事务保证',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_gateway_user_name (user_id, name),
    KEY idx_gateway_user_enabled (user_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户自带LLM网关(BYOK):密文存key,脱敏回显';
