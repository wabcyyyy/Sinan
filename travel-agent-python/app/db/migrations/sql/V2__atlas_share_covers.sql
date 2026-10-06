-- V2：行程封面 / 收藏归档 / 公开分享 / 城市字典（atlas_share_covers）
--
-- 纪律（同 V1）：一经发布不得再改；后续变更一律新增 V3__*.sql。
-- 执行链：Alembic revision `0002_atlas_share_covers` 按序执行本文件（不复制、不改写）；
--   空库 `alembic upgrade head` 依次跑 V1→V2；Flyway 时代老库先 `stamp 0001` 再 upgrade。
--
-- 注意：MySQL DDL 隐式提交 → 本文件不是原子事务；半途失败需人工核对补齐缺失语句后
--   `alembic stamp 0002_atlas_share_covers` 收尾（无 Flyway repair 了）。

-- 1) 行程主表：封面快照三列 + 署名 + 收藏/归档 + 公开分享 token
ALTER TABLE itinerary_main
  ADD COLUMN cover_url        VARCHAR(512) NULL COMMENT '应用路径 /api/uploads/...；禁止存第三方 CDN',
  ADD COLUMN cover_source     VARCHAR(16)  NULL COMMENT 'unsplash|pexels|upload；恢复默认置 NULL（不写字面量 default）',
  ADD COLUMN cover_ref        VARCHAR(64)  NULL COMMENT '来源侧资产 id（可容纳多 provider）',
  ADD COLUMN cover_credit     TEXT         NULL COMMENT 'JSON：{author,authorUrl,license,source}',
  ADD COLUMN favorite         TINYINT(1)   NOT NULL DEFAULT 0,
  ADD COLUMN archived         TINYINT(1)   NOT NULL DEFAULT 0,
  ADD COLUMN share_token      VARCHAR(48)  NULL,
  ADD COLUMN share_expires_at DATETIME     NULL,
  ADD UNIQUE KEY uk_itinerary_share_token (share_token);

-- 2) 版本快照：修正陈旧注释（V1 里的 'snapshot/apply/restore' 与真实取值不符；
--    实际 operation 取值 13 种，来源为各写路径 create_snapshot 调用点 + HTTP 默认值）
ALTER TABLE itinerary_version
  MODIFY COLUMN operation VARCHAR(24) NOT NULL DEFAULT 'snapshot'
  COMMENT 'create/generate/add_item/update_item/move_item/delete_item/reorder/nl_edit/delete/apply_plans/apply_hotel/snapshot/restore';

-- 3) 城市→国家字典：Atlas 归国的唯一权威源 + 坐标兜底（不再是坐标主来源）
CREATE TABLE IF NOT EXISTS city_geo (
  city_name     VARCHAR(64)  NOT NULL,
  country       VARCHAR(64)  NOT NULL,
  country_code  CHAR(2)      NOT NULL,
  lat           DECIMAL(10,7) NULL COMMENT '仅兜底：行程无可用坐标时使用；未核实来源前不填',
  lng           DECIMAL(10,7) NULL,
  is_domestic   TINYINT(1)   NOT NULL DEFAULT 0 COMMENT 'country_code=CN',
  PRIMARY KEY (city_name),
  KEY idx_city_geo_country (country_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='城市→国家字典（Atlas 归国 / 海外判定单一来源）';

-- 4) 种子：与 DDL 同文件，避免漏跑一个文件的机会性遗漏。
--    覆盖 = /api/itinerary/supported-cities（poi_knowledge distinct，当前 6 城 + 巴厘岛种子）
--           ∪ 评测/演示城市（京都、巴黎）∪ 常见海外目的地下限（东京/大阪/曼谷/清迈/新加坡/首尔/罗马）。
--    坐标一律 NULL：兜底坐标必须来自真实来源（poi_knowledge 质心或 Nominatim 复核），
--    不得凭记忆填；缺城后续 INSERT 或新迁移。未命中字典的城市在 Atlas 计入 dictMiss，
--    禁止默认按 CN 处理（否则海外行程被悄悄并入中国）。
INSERT INTO city_geo (city_name, country, country_code, is_domestic) VALUES
  ('北京', '中国', 'CN', 1),
  ('上海', '中国', 'CN', 1),
  ('成都', '中国', 'CN', 1),
  ('西安', '中国', 'CN', 1),
  ('三亚', '中国', 'CN', 1),
  ('杭州', '中国', 'CN', 1),
  ('巴厘岛', '印度尼西亚', 'ID', 0),
  ('京都', '日本', 'JP', 0),
  ('东京', '日本', 'JP', 0),
  ('大阪', '日本', 'JP', 0),
  ('首尔', '韩国', 'KR', 0),
  ('曼谷', '泰国', 'TH', 0),
  ('清迈', '泰国', 'TH', 0),
  ('新加坡', '新加坡', 'SG', 0),
  ('巴黎', '法国', 'FR', 0),
  ('罗马', '意大利', 'IT', 0);
