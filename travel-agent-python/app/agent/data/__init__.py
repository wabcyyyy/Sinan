"""外部事实面域：只取数据、不下结论。

阶梯位置：``runtime`` 之上、``grounding`` 之下——只准 import ``app.agent.core`` /
``app.agent.runtime`` 与 stdlib / ``app.common``，不得 import 任何上层域
（grounding / tools / research / generation / editing）。

- ``places``：OpenTripMap 为主、Nominatim 兜底（只取数据，不下结论）；
- ``web_search`` / ``pricing``：DashScope 百炼联网搜索（补池 / 实时价）；通道没有
  ``enable_search`` 插件时按「未联网」如实降级（``web_search.search_channel_ready``）；
- ``weather``：Open-Meteo 城市级预报（免费免 key，失败静默）；
- ``route_service``：路线服务适配层；
- ``map_link``：地图核实深链与 GCJ-02 偏移；
- ``city_reference``：城市级参考数据（city_geo 字典 / 消费基准，MySQL 只读）；
- ``city_center``：城市名 → 中心坐标的唯一入口（tools 与 weather 共用）。

"是否真的存在 / 是不是同一个点位"这类**结论**不在这里：判定在 ``grounding`` 域，
名称归一与距离门槛在 ``core.poi_identity``。

不设 re-export 面：域内符号按子模块路径导入。
"""
