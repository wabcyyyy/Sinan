"""生成链路的纯规则层（最底层）：不 import 任何兄弟层。

- ``generation_core``：产品口径与确定性 helper 的唯一真相源（住宿节奏、摊铺、预算估算、
  脏项与模型申报字段清洗）；
- ``budget``：预算档位与约束句、餐价钳制、0 价补水；
- ``transfer_time``：相邻点位转场时间判据（estimate_transfer_minutes 等时间线解析
  helper，2026-10-08 自 content/reflect 下沉）与生成后确定性微调 fix_transfer_gaps。

新增一个纯计算规则（住宿节奏/摊铺/折算）放这里，抄 ``generation_core`` 的无依赖风格。
"""
