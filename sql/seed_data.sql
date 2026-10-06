-- 城市级消费基准种子（城市→餐饮/交通/酒店价格档，预算引擎的缺省依据）。
-- 语料库已随 V4 迁移退役（2026-09-17）：poi_knowledge 的种子/采集管线一并删除，
-- 景点事实改走 LLM + 联网搜索 + OpenTripMap（见 travel-agent-python/app/agent/places.py）。
USE travel_assistant;

INSERT INTO city_consumption (city, level, meal_price, transport_price, hotel_price) VALUES
    ('北京', 'high', 90.00, 50.00, 500.00),
    ('上海', 'high', 95.00, 55.00, 520.00),
    ('成都', 'standard', 55.00, 35.00, 300.00),
    ('西安', 'standard', 50.00, 30.00, 280.00),
    ('三亚', 'high', 80.00, 40.00, 450.00);
