-- V4：退役 POI 权威语料库（AI-native 改造，2026-09）
--
-- 背景：检索/事实层切换为「LLM 提名 + 联网搜索 + OpenTripMap（坐标/分类/图片）
-- + Nominatim（点名解析兜底）+ 地图深链核实」，本地不再维护会腐烂的景点语料。
-- ticket_price / avg_cost / duration_min / open_time 等事实字段改由 LLM 估价
-- （itinerary_item.value_kind=estimated）并引导用户出发前经地图链接核实。
--
-- 删除两张表：
--   poi_knowledge    景点/餐饮/酒店语料（原 RAG 索引的唯一数据源）
--   hotel_room_type  酒店房型价目（外键挂在 poi_knowledge 上，随库退役；
--                    酒店候选改走联网搜索 + 基础房型估价合成）
--
-- 保留：city_geo（城市字典/国内海外判定）、city_consumption（城市消费基准）
-- ——城市级事实不随语料腐烂，仍是本地权威。
-- 已生成行程不受影响：itinerary_item 为快照式落库，不外键引用 poi_knowledge。

DROP TABLE IF EXISTS hotel_room_type;
DROP TABLE IF EXISTS poi_knowledge;
