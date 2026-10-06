# SOURCES.md — 外源清单（单一真源）

外接数据源与深链出口的唯一登记处（LA1）。判定口径（Hotellook 事故教训）：
**401/403 是活着（缺 key），404 才是死了**；连不上只说明本机网络层问题——本机
DNS 走 fake-IP 代理，须 DoH（`https://dns.google/resolve?name=<host>&type=A`）
取真实 IP 后 `curl --resolve <host>:443:<ip> <url>` 直连复核，排除代理假象再下结论。

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
| open-meteo | 城市级天气预报 | `https://api.open-meteo.com/v1/forecast` | 无 key 免费（非商用） | `ok`（200） | 2026-09-25 | 探针 |
| nominatim | 点名→坐标兜底（地理编码） | `https://nominatim.openstreetmap.org/search` | 无 key；政策 1 rps + 可联系 UA | `ok`（200） | 2026-09-25 | 探针报 unreachable（代理 SSL 断连）→ DoH 直连复核 200，不判死 |
| opentripmap | 景点坐标/分类/图片 | `https://api.opentripmap.com/0.1/en`（`/places/geoname`、`/places/radius`、`/places/xid/{id}`） | 免费 key；限速 <5 rps（我们 geo/radius 0.35s、detail 1.0s 间隔） | `auth-required`（401=活着，缺 apikey） | 2026-09-25 | 探针 |
| travelpayouts | 机票缓存价（Aviasales v3） | `https://api.travelpayouts.com/aviasales/v3/prices_for_dates` | token 免费（affiliate 注册）；官方限速 600 req/min | `auth-required`（401=活着，缺 token） | 2026-09-25 | 探针 |
| serpapi | 按需实时报价（`engine=google_flights` 航班 + `engine=google_hotels` 酒店，LA2 起两引擎共用同一 key 与月配额） | `https://serpapi.com/search.json` | 免费 250 次/月（硬顶，只够按需按钮，不进主链路） | `auth-required`（401=活着，缺 key） | 2026-09-25 | 探针 |
| travelpayouts-data | IATA 映射存在性校验（**LA3 离线快照**：`scripts/iata_snapshot.py` 手工刷新，不进探针/门禁——静态数据集，非运行时通道） | `https://api.travelpayouts.com/data/airports.json`、`https://api.travelpayouts.com/data/cities.json`、`https://api.travelpayouts.com/data/routes.json` | 免 key（静态 JSON，与 token 通道无关） | `ok`（均 200） | 2026-09-26 | LA3 快照脚本直连 |

## 深链出口（浏览器侧可用性，非数据 API）

| 源 | 用途 | 端点 | 免费额度 | 结论 | 最后验证 | 验证方式 |
|---|---|---|---|---|---|---|
| aviasales 深链 | 机票核实跳转（`deep_link` 基座） | `https://www.aviasales.com` | — | `ok`（200） | 2026-09-25 | 探针 |
| 高德 URI 深链 | 国内单点/搜索/路线核实（**LA2 起兼作酒店候选卡核实出口**） | `https://uri.amap.com/marker`、`https://uri.amap.com/search`、`https://uri.amap.com/navigation` | 免 key | `ok`（均 200；`uri.amap.com` → `ditu.amap.com` 落地正常） | 2026-09-25 | 探针 |
| Google Maps 深链 | 海外单点/路线核实（**LA2 起兼作酒店候选卡核实出口**） | `https://www.google.com/maps/search/`、`https://www.google.com/maps/dir/` | 免 key | `ok`（均 200；经本机出口，海外直连环境另议） | 2026-09-25 | 探针 |

## 不做自动探针的通道

| 源 | 用途 | 端点 | 额度 | 不探针的理由 |
|---|---|---|---|---|
| DashScope 联网搜索 | 研究证据/备选池补齐（`enable_search` 插件） | `llm_base_url`（`app/common/config.py`，不在 data 域） | LLM 按 token 计费 | 探针需真实计费调用；通道没有该插件（DeepSeek 官方等）时按「未联网」如实降级并留一条可定位 warning（`web_search.search_channel_ready`），不再拿模型记忆冒充检索结果——能力判否一次外呼都不发，没有"mock 盲区" |

## 候选源（已侦察未接入；接入时补"结论/最后验证"列）

| 源 | 用途 | 端点 | 免费额度 | 计划卡 |
|---|---|---|---|---|
| openrouteservice | 城内交通真路线 | `https://api.openrouteservice.org/v2/directions`（…`/{profile}`） | 免费 2,000 次/天、40/分钟；高校/非营利可申请提额 | LA5 |

## 已退役（勿再考虑）

| 源 | 结论 | 核实日期 |
|---|---|---|
| Hotellook 酒店缓存价（`engine.hotellook.com/api/v2/cache.json`） | `dead`：404 含根路径，DoH 真实 IP 直连复核仍 404；**LA2 已从代码摘除**（主链路回退 estimated + 地图核实深链，「查实时价」改走 serpapi google_hotels） | 2026-09-25 |
| Hotellook 搜索深链（`search.hotellook.com/hotels`） | 改嫁：302 → `hotels-api.aviasales.ru/v1/tp/redirect` → 403（带/不带 marker 同）；`www.aviasales.com/hotels/<city>` 亦 404——Aviasales 系酒店深链整体不可用，LA2 改挂地图核实深链 | 2026-09-25 |
| Amadeus Self-Service | 2026-07-17 整体退役 | 2026-09-25（官方公告） |
| Kiwi.com Tequila | 2024-05-30 起仅认证 B2B，普通 key 亦 403 | 2026-09-25（官方文档） |
