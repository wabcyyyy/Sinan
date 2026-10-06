-- V7：模板发布（SPEC C2.4，方案 A——发布快照物化进主表列）
--
-- 纪律（同 V1-V6）：一经发布不得再改；后续变更一律新增 V8__*.sql。
-- 执行链：Alembic revision `0007_template` 按序执行本文件（不复制、不改写）。
--
-- template_summary 存脱敏投影（TemplateSummaryVO 物化）：不含 expense/成员/
-- userId/备注；fork 只读这份快照，不回读源行程，源行程后续编辑不影响已发布模板。

ALTER TABLE itinerary_main
  ADD COLUMN template_published_at DATETIME NULL COMMENT '发布时间；NULL=未发布',
  ADD COLUMN template_summary JSON NULL COMMENT '脱敏投影快照（模板广场/fork 数据源）';
