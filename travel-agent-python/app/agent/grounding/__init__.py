"""结论层域：存在性判定 / 证据票 / 溯源标签。

阶梯位置：``data`` 之上、``tools`` 之下——只准 import ``app.agent.core`` /
``app.agent.runtime`` / ``app.agent.data`` 与 stdlib / ``app.common``，不得 import
任何上层域（tools / research / generation / editing）。

- ``existence``：存在性解析器（可插拔 provider 的三值判定：证实/证伪/未判定）+ 同城硬闸；
- ``existence_commercial``：付费 provider（高德 / Google Places），无 key 一律如实未判定；
- ``facts``：事实落地——把点位名变成带真实坐标与来源的事实（``has_coord`` / ``local_ground``，
  原 ``grounding`` 模块；包化时改名，避免 ``grounding.grounding``）；
- ``grounding_evidence``：证据票（"本服务真的抓过这一行"的不可伪造凭据）；
- ``grounding_labels``：背书口径唯一实现（来源值域 + 证据校验 → 溯源标签）；
- ``suggestion_grounding``：备选池的批量后验证。

**只下结论，不取数据**：外部取数在 ``data`` 域；"是不是同一个点位"的纯判定在
``core.poi_identity``。三条纪律：未判定与"解析到别的城市"原样保留（绝不把"没查到"
当成"不存在"）；``source`` 只由本域写入；标签只准有这一份实现。

不设 re-export 面：域内符号按子模块路径导入。
"""
