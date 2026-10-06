"""业务面（/api/** 非 agent 前缀）请求/响应契约的集中地（G-1.1 契约单一源）。

职责：
- 收拢原散落在 app/api/business/*（路由文件内的 Body 类）与 app/services/*
  （GenerateTripRequest/ItemUpsertRequest/HotelOptionRequest）的线级模型，
  使 scripts/export_contracts.py 有唯一取数点；路由与服务层一律从本包 import。

边界（本期有意不建模，勿「顺手补全」）：
- itinerary_query.detail() 等 ORM + metadata_json 动态组装的详情/列表 VO：
  字段集随元数据透传开放，强建第二份副本必然漂移（违反「单一真源」哲学）；
  前端按「字段存在才渲染」防御式消费，维持现状。
- /api/admin/**、/api/agent/v1/metrics|usage|runs 等治理诊断面的快照 dict：
  多点装配的运维指标，无跨语言消费方，暂不入契约组。
"""
