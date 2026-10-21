# SOURCES.md — 外源清单（单一真源）

外接数据源与深链出口的唯一登记处。401/403 记录为需要鉴权，404 记录为当前端点不可用；
连接失败不能直接判定服务退役。使用代理或 fake-IP DNS 时，可通过 DoH
（`https://dns.google/resolve?name=<host>&type=A`）取得真实 IP，再用
`curl --resolve <host>:443:<ip> <url>` 复核。下表状态是对应日期的历史观测，不代表当前可用性。

- 探针：`uv run python scripts/probe_sources.py`（手工 / 夜间跑；**不进 just check**，
  CI 无外网是既有假设）；
- 机检：`tests/test_sources_manifest.py` 保证 `app/agent/data/*.py` 里的每个端点
  常量都被本清单覆盖（方向：**代码 ⊆ 文档**；本文可以登记尚未接入的候选源与已退役源）；
- 纪律：结论列只认探针/直连实测，不认记忆；**最后验证列必填实测当日日期**——
  探针靠人工触发会忘，遗忘靠这列显式化（LA1 风险条款）；真实 key 只进本地
  `.env`，本清单永不登记 token。

## 数据源 API

| 源 | 用途 | 端点 | 免费额度 | 结论 | 最后验证 | 验证方式 |
|---|---|---|---|---|---|---|
| DeepSeek Search | 真实网页检索与候选来源（不证明地点字段正确） | `https://api.deepseek.com/v1/tools/search` | 当前官方按成功且非空请求 5 元/千次；与模型 Token 分开 | `ok`（200，4 条含正文结果） | 2026-10-21 | 手工单次契约调用，查询“杭州 西湖 官方 游览”；需要配置通道 key，因计费不做自动探针 |
| open-meteo | 城市级天气预报 | `https://api.open-meteo.com/v1/forecast` | 无 key 免费（非商用） | `ok`（200） | 2026-09-25 | 探针 |
| nominatim | 点名→坐标兜底（地理编码） | `https://nominatim.openstreetmap.org/search` | 无 key；政策 1 rps + 可联系 UA | `ok`（200） | 2026-09-25 | 探针报 unreachable（代理 SSL 断连）→ DoH 直连复核 200，不判死 |
| overpass | 按 OSM 标签检索（diet:vegetarian/vegan 餐饮池，饮食证据检索面） | `https://overpass-api.de/api/interpreter` | 无 key 免费；自约束 1.1s 间隔 + 单次 25s timeout | `ok`（200） | 2026-10-11 | 隔离验收实测：新宿 bbox diet 检索 6 行（curl POST form） |
| opentripmap | 景点坐标/分类/图片 | `https://api.opentripmap.com/0.1/en`（`/places/geoname`、`/places/radius`、`/places/xid/{id}`） | 免费 key；限速 <5 rps（我们 geo/radius 0.35s、detail 1.0s 间隔） | `auth-required`（401=活着，缺 apikey） | 2026-09-25 | 探针 |
| travelpayouts | 机票缓存价（Aviasales v3） | `https://api.travelpayouts.com/aviasales/v3/prices_for_dates` | token 免费（affiliate 注册）；官方限速 600 req/min | `auth-required`（401=活着，缺 token） | 2026-09-25 | 探针 |
| serpapi | 按需实时报价（`engine=google_flights` 航班 + `engine=google_hotels` 酒店，LA2 起两引擎共用同一 key 与月配额） | `https://serpapi.com/search.json` | 免费 250 次/月（硬顶，只够按需按钮，不进主链路） | `auth-required`（401=活着，缺 key） | 2026-09-25 | 探针 |
| travelpayouts-data | IATA 映射存在性校验（**LA3 离线快照**：`scripts/iata_snapshot.py` 手工刷新，不进探针/门禁——静态数据集，非运行时通道） | `https://api.travelpayouts.com/data/airports.json`、`https://api.travelpayouts.com/data/cities.json`、`https://api.travelpayouts.com/data/routes.json` | 免 key（静态 JSON，与 token 通道无关） | `ok`（均 200） | 2026-09-26 | LA3 快照脚本直连 |

## 深链出口（浏览器侧可用性，非数据 API）

饮食字段证据的实体来源链接使用 `https://www.openstreetmap.org/` 下的 `node/{id}`、
`way/{id}` 或 `relation/{id}` 页面。它们是 Nominatim/Overpass 结果对应的 OSM 实体页，
不是新增抓取 API，也不代表餐厅当前菜单。点名解析携带 `extratags=1`，仅把
`diet:vegetarian` / `diet:vegan` 的 `yes` 或 `only` 记为饮食依据；`limited`、缺失
不算已证实，菜单仍须出发前复核。同一批 extratags 里的类别标签（amenity/religion/
tourism/leisure/historic）与 jsonv2 主分类同样只做**原样搬运**成字段证据，是否
构成排除类别违例由确定性检查器判定。实体页未另做可用性探针，不能据此宣称实时菜单可达。
参数与标签口径见 [Nominatim Search](https://nominatim.org/release-docs/latest/api/Search/)
和 [OSM diet 标签](https://wiki.openstreetmap.org/wiki/Key%3Adiet%3A%2A)。

| 源 | 用途 | 端点 | 免费额度 | 结论 | 最后验证 | 验证方式 |
|---|---|---|---|---|---|---|
| aviasales 深链 | 机票核实跳转（`deep_link` 基座） | `https://www.aviasales.com` | — | `ok`（200） | 2026-09-25 | 探针 |
| 高德 URI 深链 | 国内单点/搜索/路线核实（**LA2 起兼作酒店候选卡核实出口**） | `https://uri.amap.com/marker`、`https://uri.amap.com/search`、`https://uri.amap.com/navigation` | 免 key | `ok`（均 200；`uri.amap.com` → `ditu.amap.com` 落地正常） | 2026-09-25 | 探针 |
| Google Maps 深链 | 海外单点/路线核实（**LA2 起兼作酒店候选卡核实出口**） | `https://www.google.com/maps/search/`、`https://www.google.com/maps/dir/` | 免 key | `ok`（均 200；经本机出口，海外直连环境另议） | 2026-09-25 | 探针 |

## 不做自动探针的通道

| 源 | 用途 | 端点 | 额度 | 不探针的理由 |
|---|---|---|---|---|
| DashScope 联网搜索 | 研究证据/备选池补齐（`enable_search` 插件） | `llm_base_url`（`app/common/config.py`，不在 data 域） | LLM 按 token 计费 | 探针需真实计费调用；通道既没有该插件也没有独立 Search API 时按「未联网」如实降级并留一条可定位 warning（`web_search.search_channel_ready`），不再拿模型记忆冒充检索结果——能力判否一次外呼都不发，没有"mock 盲区" |

## 已退役（勿再考虑）

| 源 | 结论 | 核实日期 |
|---|---|---|
| Hotellook 酒店缓存价（`engine.hotellook.com/api/v2/cache.json`） | `dead`：404 含根路径，DoH 真实 IP 直连复核仍 404；**LA2 已从代码摘除**（主链路回退 estimated + 地图核实深链，「查实时价」改走 serpapi google_hotels） | 2026-09-25 |
| Hotellook 搜索深链（`search.hotellook.com/hotels`） | 改嫁：302 → `hotels-api.aviasales.ru/v1/tp/redirect` → 403（带/不带 marker 同）；`www.aviasales.com/hotels/<city>` 亦 404——Aviasales 系酒店深链整体不可用，LA2 改挂地图核实深链 | 2026-09-25 |
| Amadeus Self-Service | 2026-07-17 整体退役 | 2026-09-25（官方公告） |
| Kiwi.com Tequila | 2024-05-30 起仅认证 B2B，普通 key 亦 403 | 2026-09-25（官方文档） |
