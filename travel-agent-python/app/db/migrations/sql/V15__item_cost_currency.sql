-- V15：行程项费用币种（条目 cost 的币种显式化，见 app/agent/data/fx.py normalize_item_cost）
--
-- 纪律（同 V1-V14）：一经入库不得再改；后续变更一律新增 V16__*.sql。
-- 执行链：Alembic revision `0015_item_cost_currency` 按序执行本文件。
--
-- 为什么建列：此前 cost 的币种只靠「一律人民币」的提示约定，海外行程模型写进本币数字时
-- 只能按金额大小猜（1500 元的人民币餐费被当成日元压成 72 元）。现在模型显式声明币种、
-- 后端按固定汇率换算，cost 恒为人民币；本列记录 cost 的币种是否有据：
-- 'CNY' = 有据（模型声明后已换算 / 用户手填）；NULL = 未声明（存量行与旧口径，按人民币理解）。
-- 存量行一律 NULL，不回填：历史数据确实没有声明过币种，回填 'CNY' 等于事后伪造证据。

ALTER TABLE itinerary_item
    ADD COLUMN cost_currency VARCHAR(3) NULL COMMENT 'cost 的币种(V15):CNY=有据,NULL=未声明按人民币理解' AFTER cost;
