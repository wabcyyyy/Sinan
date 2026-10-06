-- V1 基线：当前完整 schema（等价 schema.sql 的最终态 + 21 个 add_*.sql 全部折叠）。
--
-- 来源与治理：
--   * 由 sql/schema.sql 机械生成（剥离 DROP TABLE / CREATE DATABASE / USE，迁移不得破坏性重建）；
--   * 已机械核对 21 个 sql/add_*.sql|align_*.sql 引入的表/列/索引全部包含在本文件中（无 MODIFY/DROP 类增量）；
--   * 原始增量脚本已归档到 sql/archive/，历史库首次接入由 baseline-on-migrate 打基线跳过本文件。
-- 修改纪律：本文件一经发布不得再改（Flyway 校验 checksum）；后续变更一律新增 V2__*.sql。



CREATE TABLE sys_user (
    id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    username    VARCHAR(64)  NOT NULL COMMENT '用户名',
    password    VARCHAR(128) NOT NULL COMMENT 'BCrypt 加密密码',
    nickname    VARCHAR(64)  DEFAULT NULL COMMENT '昵称',
    phone       VARCHAR(20)  DEFAULT NULL COMMENT '手机号',
    status      TINYINT      NOT NULL DEFAULT 1 COMMENT '状态 1-正常 0-禁用',
    role        VARCHAR(20)  NOT NULL DEFAULT 'user' COMMENT '角色 user-普通用户 admin-管理员',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted     TINYINT      NOT NULL DEFAULT 0 COMMENT '逻辑删除 0-未删 1-已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username)
) ENGINE = InnoDB COMMENT '用户表';

CREATE TABLE user_preference (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    user_id       BIGINT       NOT NULL,
    pref_label    VARCHAR(32)  NOT NULL COMMENT '偏好标签（如 人文历史/美食）',
    count         INT          NOT NULL DEFAULT 1 COMMENT '累计选择次数',
    source        VARCHAR(24)  NOT NULL DEFAULT 'explicit' COMMENT '来源 explicit/inferred/feedback',
    confidence    DECIMAL(5,4) NOT NULL DEFAULT 1.0000 COMMENT '偏好置信度',
    negative      TINYINT      NOT NULL DEFAULT 0 COMMENT '是否为负反馈',
    hard_constraint TINYINT    NOT NULL DEFAULT 0 COMMENT '是否为硬约束',
    last_seen_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_pref (user_id, pref_label),
    KEY idx_user (user_id)
) ENGINE = InnoDB COMMENT '用户偏好统计';

CREATE TABLE itinerary_version (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    itinerary_id    BIGINT       NOT NULL,
    user_id         BIGINT       NOT NULL,
    parent_version_id BIGINT     DEFAULT NULL,
    version_no      INT          NOT NULL,
    operation       VARCHAR(24)  NOT NULL DEFAULT 'snapshot' COMMENT 'snapshot/apply/restore',
    summary         VARCHAR(255) DEFAULT NULL,
    snapshot_json   LONGTEXT     NOT NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_itinerary_version (itinerary_id, version_no),
    KEY idx_version_itinerary (itinerary_id, id)
) ENGINE = InnoDB COMMENT '行程版本快照';

CREATE TABLE itinerary_main (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    user_id     BIGINT       NOT NULL COMMENT '所属用户',
    title       VARCHAR(128) NOT NULL COMMENT '行程标题',
    city        VARCHAR(64)  NOT NULL COMMENT '目的地城市',
    start_date  DATE         DEFAULT NULL COMMENT '开始日期',
    end_date    DATE         DEFAULT NULL COMMENT '结束日期',
    days        INT          NOT NULL DEFAULT 1 COMMENT '行程天数',
    persons     INT          NOT NULL DEFAULT 1 COMMENT '出行人数',
    budget      DECIMAL(12, 2) DEFAULT NULL COMMENT '用户预算上限',
    preferences VARCHAR(512) DEFAULT NULL COMMENT '偏好标签，逗号分隔',
    hotel_tier  VARCHAR(16) DEFAULT NULL COMMENT '住宿档次偏好',
    stay_nights INT NOT NULL DEFAULT 0 COMMENT '住宿晚数',
    status      TINYINT      NOT NULL DEFAULT 1 COMMENT '1-草稿 2-已生成 3-已取消',
    plan_note   TEXT         DEFAULT NULL COMMENT 'AI管家规划讲解',
    suggestions_json TEXT    DEFAULT NULL COMMENT '备选池（发现更多）JSON，来自生成时候选池中未排入行程的优质点位',
    trip_theme   VARCHAR(64) DEFAULT NULL COMMENT '整趟主题标题（来自生成契约 trip_theme）',
    gen_state      VARCHAR(16) DEFAULT NULL COMMENT '生成状态机：GENERATING/PARTIAL/COMPLETED/FAILED，NULL=迁移前存量',
    gen_started_at DATETIME    DEFAULT NULL COMMENT '本次生成开始时间（含恢复续跑刷新）',
    gen_finished_at DATETIME   DEFAULT NULL COMMENT '本次生成结束时间',
    gen_resumed    TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '已自动续跑过一次（防失败-重生成死循环）',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted     TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_user (user_id),
    KEY idx_city (city)
) ENGINE = InnoDB COMMENT '行程主表';

CREATE TABLE itinerary_chat_message (
    id                 BIGINT       NOT NULL AUTO_INCREMENT,
    itinerary_id       BIGINT       NOT NULL,
    user_id            BIGINT       NOT NULL,
    role               VARCHAR(16)  NOT NULL COMMENT 'user/ai',
    content            TEXT         NOT NULL,
    plans_json         LONGTEXT     DEFAULT NULL,
    hotel_options_json LONGTEXT     DEFAULT NULL,
    changed            TINYINT      NOT NULL DEFAULT 0,
    created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_itinerary_user (itinerary_id, user_id, id)
) ENGINE = InnoDB COMMENT '行程对话记忆';

CREATE TABLE itinerary_day (
    id            BIGINT      NOT NULL AUTO_INCREMENT,
    itinerary_id  BIGINT      NOT NULL COMMENT '行程主表 ID',
    day_no        INT         NOT NULL COMMENT '第几天，从 1 开始',
    travel_date   DATE        DEFAULT NULL COMMENT '当日日期',
    city          VARCHAR(64) DEFAULT NULL COMMENT '当日所在城市',
    note          VARCHAR(512) DEFAULT NULL COMMENT '当日备注',
    metadata_json LONGTEXT DEFAULT NULL COMMENT '每日手册元数据 JSON（主题/路线/备选/拍照点/提醒）',
    generation_action_id VARCHAR(96) DEFAULT NULL COMMENT '稳定的每日生成动作 ID',
    generation_fingerprint CHAR(64) DEFAULT NULL COMMENT '生成参数 SHA-256 指纹',
    generation_status VARCHAR(24) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/RUNNING/SUCCEEDED/FAILED/TIMED_OUT_UNKNOWN',
    generation_error VARCHAR(512) DEFAULT NULL COMMENT '最近一次生成错误',
    created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted       TINYINT     NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_itinerary_day_no (itinerary_id, day_no),
    UNIQUE KEY uk_generation_action (generation_action_id),
    KEY idx_itinerary (itinerary_id),
    KEY idx_generation_status (generation_status)
) ENGINE = InnoDB COMMENT '日行程表';

CREATE TABLE itinerary_item (
    id            BIGINT         NOT NULL AUTO_INCREMENT,
    day_id        BIGINT         NOT NULL COMMENT '日行程 ID',
    itinerary_id  BIGINT         NOT NULL,
    item_type     VARCHAR(16)    NOT NULL COMMENT 'attraction-景点 food-餐饮 hotel-酒店 transport-交通',
    poi_name      VARCHAR(128)   NOT NULL COMMENT '地点名称',
    poi_id        VARCHAR(64)    DEFAULT NULL COMMENT '高德 POI ID',
    address       VARCHAR(255)   DEFAULT NULL,
    latitude      DECIMAL(10, 6) DEFAULT NULL,
    longitude     DECIMAL(10, 6) DEFAULT NULL,
    start_time    TIME           DEFAULT NULL COMMENT '计划开始时间',
    end_time      TIME           DEFAULT NULL COMMENT '计划结束时间',
    duration_min  INT            DEFAULT NULL COMMENT '建议游玩时长(分钟)',
    cost          DECIMAL(10, 2) DEFAULT NULL COMMENT '预估单价（景点/餐饮按人，酒店按间/晚）',
    tag           VARCHAR(32)    DEFAULT NULL COMMENT '标签：亲子/网红/人文等',
    remark        VARCHAR(255)   DEFAULT NULL,
    open_time     VARCHAR(64)    DEFAULT NULL COMMENT '来源营业时间文本',
    image_url     VARCHAR(1024)  DEFAULT NULL COMMENT '地点图片 URL（可选）',
    source        VARCHAR(128)   DEFAULT NULL COMMENT '本次事实来源标识',
    source_updated_at DATETIME   DEFAULT NULL COMMENT '来源数据更新时间',
    verification_status VARCHAR(24) NOT NULL DEFAULT 'unverified' COMMENT 'verified/partially_verified/unverified',
    value_kind    VARCHAR(16)    NOT NULL DEFAULT 'generated' COMMENT 'observed/estimated/generated',
    freshness_status VARCHAR(16) NOT NULL DEFAULT 'unknown' COMMENT 'fresh/stale/unknown',
    review_requirement VARCHAR(24) NOT NULL DEFAULT 'before_departure' COMMENT 'none/before_departure',
    fact_evidence_json LONGTEXT   DEFAULT NULL COMMENT '字段级事实证据 JSON',
    intro         VARCHAR(600)   DEFAULT NULL COMMENT '景点详细介绍',
    why_note      VARCHAR(255)   DEFAULT NULL COMMENT '叙事理由（why_this，value_kind=generated）',
    sort_no       INT            NOT NULL DEFAULT 0 COMMENT '当日排序',
    created_at    DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted       TINYINT        NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_day (day_id),
    KEY idx_itinerary (itinerary_id)
) ENGINE = InnoDB COMMENT '行程项表';

CREATE TABLE budget_detail (
    id           BIGINT         NOT NULL AUTO_INCREMENT,
    itinerary_id BIGINT         NOT NULL,
    day_id       BIGINT         DEFAULT NULL COMMENT '可为空表示整行程汇总',
    category     VARCHAR(16)    NOT NULL COMMENT '门票/餐饮/酒店/交通',
    amount       DECIMAL(12, 2) NOT NULL COMMENT '该分类金额(总价)',
    item_count   INT            NOT NULL DEFAULT 0 COMMENT '明细条数',
    remark       VARCHAR(255)   DEFAULT NULL,
    created_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    deleted      TINYINT        NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_itinerary (itinerary_id)
) ENGINE = InnoDB COMMENT '预算明细表';

CREATE TABLE poi_knowledge (
    id            BIGINT         NOT NULL AUTO_INCREMENT,
    city          VARCHAR(64)    NOT NULL COMMENT '所属城市',
    name          VARCHAR(128)   NOT NULL COMMENT '景点/地点名称',
    category      VARCHAR(16)    NOT NULL DEFAULT 'attraction' COMMENT 'attraction/food/hotel',
    address       VARCHAR(255)   DEFAULT NULL,
    latitude      DECIMAL(10, 6) DEFAULT NULL,
    longitude     DECIMAL(10, 6) DEFAULT NULL,
    ticket_price  DECIMAL(10, 2) DEFAULT NULL COMMENT '门票参考价，NULL 表示免费或未知',
    avg_cost      DECIMAL(10, 2) DEFAULT NULL COMMENT '人均消费(元)：餐饮人均/景点园内平均花费，非门票价',
    duration_min  INT            DEFAULT NULL COMMENT '建议游玩时长(分钟)',
    open_time     VARCHAR(64)    DEFAULT NULL COMMENT '开放时间文本',
    tags          VARCHAR(128)   DEFAULT NULL COMMENT '偏好标签，逗号分隔',
    rating        DECIMAL(2, 1)  DEFAULT NULL COMMENT '评分 0-5',
    description   VARCHAR(512)   DEFAULT NULL,
    source        VARCHAR(128)   NOT NULL DEFAULT 'mysql.poi_knowledge' COMMENT '权威数据来源',
    source_updated_at DATETIME   DEFAULT NULL COMMENT '来源数据更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_city_name_category (city, name, category),
    KEY idx_city (city),
    KEY idx_tags (tags)
) ENGINE = InnoDB COMMENT '景点知识库(种子数据，幻觉检测对照)';

CREATE TABLE hotel_room_type (
    id          BIGINT         NOT NULL AUTO_INCREMENT,
    poi_id      BIGINT         NOT NULL COMMENT '关联 poi_knowledge 酒店',
    room_name   VARCHAR(128)   NOT NULL COMMENT '房型名称',
    base_price  DECIMAL(10, 2) NOT NULL COMMENT '每晚单间参考基准价',
    capacity    INT            NOT NULL DEFAULT 2 COMMENT '建议入住人数',
    bed_type    VARCHAR(64)    DEFAULT NULL,
    breakfast   VARCHAR(64)    DEFAULT NULL,
    description VARCHAR(512)   DEFAULT NULL,
    is_default  TINYINT        NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk_hotel_room (poi_id, room_name),
    KEY idx_hotel (poi_id)
) ENGINE = InnoDB COMMENT '酒店房型与参考价格';

CREATE TABLE city_consumption (
    id          BIGINT         NOT NULL AUTO_INCREMENT,
    city        VARCHAR(64)    NOT NULL COMMENT '城市',
    level       VARCHAR(16)    NOT NULL DEFAULT 'standard' COMMENT '消费水平档位',
    meal_price  DECIMAL(10, 2) NOT NULL COMMENT '人均每餐参考(元)',
    transport_price DECIMAL(10, 2) NOT NULL COMMENT '日均市内交通(元)',
    hotel_price DECIMAL(10, 2) NOT NULL COMMENT '单间/晚参考(元)',
    PRIMARY KEY (id),
    UNIQUE KEY uk_city (city)
) ENGINE = InnoDB COMMENT '城市消费系数表(预算计算口径来源)';

CREATE TABLE export_task (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    itinerary_id BIGINT       NOT NULL COMMENT '行程ID',
    user_id      BIGINT       NOT NULL COMMENT '发起用户',
    task_type    VARCHAR(16)  NOT NULL DEFAULT 'PDF' COMMENT '任务类型',
    status       VARCHAR(16)  NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING/DONE/FAILED',
    file_path    VARCHAR(512) DEFAULT NULL COMMENT '产物文件路径',
    error_msg    VARCHAR(512) DEFAULT NULL COMMENT '失败原因',
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  DATETIME     DEFAULT NULL,
    deleted      TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_itinerary (itinerary_id),
    KEY idx_user (user_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT '异步导出任务表';
