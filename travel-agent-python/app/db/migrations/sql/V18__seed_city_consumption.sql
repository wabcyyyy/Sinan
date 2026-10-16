-- V15：城市消费基准并入迁移链（原根目录外挂 sql/seed_data.sql 的种子）。
--
-- 背景（2026-10-11 仓库体检）：city_geo 由 V2/V12 种入，但 city_consumption
-- **从未被任何迁移种入**——新装库该表恒为空，city_reference.get_city_consumption()
-- 对所有城市返回 None，预算引擎静默退回代码默认值（generators.py 的 meal=60.0），
-- 「北京 90 / 成都 55」这类城市级消费差异完全失效。种子此前只存在于根目录
-- sql/seed_data.sql，靠 README 一句「可选手工导入」，不在任何自动流程里
-- （该文件与目录已于开源预览改造中删除，本迁移把种子补回唯一真相源）。
--
-- 用 INSERT IGNORE 而非裸 INSERT：README 曾指导手工导入该种子，已导入过的库
-- 再次升级会命中 uk_city 唯一键。IGNORE 只吞重复键（保留库里既有行），
-- 未知列等真实错误仍会抛出——不让迁移静默放过真问题。
--
-- 纪律同 V12：纯 INSERT、不建表。
INSERT IGNORE INTO city_consumption (city, level, meal_price, transport_price, hotel_price) VALUES
    ('北京', 'high', 90.00, 50.00, 500.00),
    ('上海', 'high', 95.00, 55.00, 520.00),
    ('成都', 'standard', 55.00, 35.00, 300.00),
    ('西安', 'standard', 50.00, 30.00, 280.00),
    ('三亚', 'high', 80.00, 40.00, 450.00);
