# 深链语义 parity 差异表(前后端地图深链)

地图深链的共享口径用 `deeplink_cases.json`(同目录,16 个共享 case)把两侧各自钉住,
任何一侧单方面改语义都会显形:

- 后端断言:`travel-agent-python/tests/test_deeplink_parity.py`(`app.agent.data.map_link` 的 `map_search_url` / `map_directions_url`)
- 前端断言:`travel-frontend-react/src/react-app/itinerary/deeplink-parity.test.ts`(`react-app/itinerary/mapPins.ts` 的 `amapLink` / `googleMapsLink`,2026-10-04 恢复同源断言)

断言粒度 = 协议 host + path 前缀 + 关键查询参数存在/缺席;`src`/`callnative`/`policy`/
`coordinate` 与坐标数值等实现细节不整串相等。两侧期望不同(协议层)的 case 在两个测试里
都必须登记说明;期望相同的 case 不允许登记——保证本表不漏记、不过期。

## 2026-10-04 机制变化(React 迁移)

React 迁移删掉了旧 Vue 端 `src/utils/geo.ts` 的搜索/路线深链实现,前端深链面收窄为
**pin 级固定双出口**:点位有**有效**坐标才出「高德 marker + 谷歌查询」两条核实链接(经
`shared/map-link` 白名单),无路线深链、无按国内外分支的语义搜索;坐标有效性谓词
`hasValidCoords` 已于 2026-10-04 拍板补齐(与后端 `_parse_coords` 同语义:
有限值 + 经纬度范围 + 非 0/0 哨兵,无效视同无坐标)。搜索/路线深链从此是
**后端单端实现**。两侧职责不对称产生的机制性差异登记如下
(R1/R3/R4,后端测试的 DIFF_NOTES 与之编号对应):

## 剩余差异(R1/R3/R4,均为"已知且接受")

| 类 | case | 后端 | 前端 | 定性 |
|------|------|------|------|------|
| R1 固定双出口 | search_domestic_with_coords、search_foreign_with_coords | 按国内外分支出单条语义链接(国内高德 marker 先换算 GCJ-02 / 海外谷歌坐标查询) | 有效坐标恒出高德 marker(原始坐标,不换算)+ 谷歌查询两条 | 已知且接受:前端只做"核实"导流,不做语义分支;GCJ-02 差异在断言粒度(不比坐标值)之外 |
| R3 无/无效坐标无出口 | search_domestic_no_coords、search_foreign_no_coords、search_hanzi_foreign_city_no_coords、search_zero_sentinel、search_out_of_range_coords | 无/无效坐标回落关键词搜索(不给假位置) | null(无/无效坐标不进 pin,自然无链接) | 已知且接受:无坐标点位在地图卡脚注如实交代,不提供猜测性链接;哨兵/越界坐标已按 R2 拍板补齐谓词视同无坐标 |
| R4 路线退役 | route_domestic_two_stops、route_foreign_two_stops、route_domestic_three_stops、route_foreign_five_stops、route_foreign_seven_stops、route_mixed_validity_stops、route_out_of_range_stop | 高德 navigation / 谷歌 dir(有效性过滤+GCJ-02) | null(无路线深链实现) | 已知且接受:逐点核实替代整段路线;route_single_point 与 route_domestic_four_stops 两侧同为 null,不登记 |

## 2026-09-18 统一决策记录(D1-D10 拍板,全部按推荐执行;2026-10-04 起部分被 R 类吸收)

| # | 原分歧 | 决策 | 落点 |
|---|--------|------|------|
| D1 | 国内搜索有坐标:后端 marker(不换算)vs 前端关键词 | 后端保留 marker 但**先换算 GCJ-02**(精确且不偏移);前端维持关键词 | `places.to_gcj02`(两侧测试各钉已知值;前端实现 2026-10-04 随 React 迁移退役,后端保留) |
| D2 | 越界坐标后端无校验、误判海外 | 后端引入与前端 `hasValidCoordinates` 同语义的 `_parse_coords`(有限值+90/180+非 0/0),判定与打点共用 | 后端已统一;前端谓词 2026-10-04 起缺失 → R2 |
| D3 | 汉字名海外城市判定相反 | 后端删除汉字兜底,无/无效坐标改查 **`city_geo.is_domestic`**;字典未收录/库不可用**默认海外** | `places._dict_domestic`;parity 测试把字典钉成固定映射测链接逻辑,DB 路径单独单测 |
| D4 | 海外搜索坐标 vs 名称 | 后端有有效坐标用 `lat,lng`,回落名称关键词 | 已统一(现仅后端) |
| D5 | 高德 via 上限 | 官方 URI 文明确认**最多 1 个途经点**:超出返回 None(界面提示分段) | 已统一(现仅后端);eval 深度指标的"全天路线达标"随之更真实 |
| D6 | 谷歌 waypoints 上限 | 后端不设上限;前端限 3 —— 前端路线深链 2026-10-04 退役,分歧被 R4 吸收 | `route_foreign_seven_stops` 现两侧差异属 R4 |
| D7 | 0/0 哨兵后端两处不一致 | 并入 D2:统一谓词后判定/打点同口径 | 已统一(现仅后端) |
| D8 | 高德路线不换算 GCJ-02 | 并入 D1:from/to/via 全部先换算 | 已统一(现仅后端) |
| D9 | 关键词拼接无分隔(前端) | 空格分隔,消除「New YorkTimes Square」类坏关键词 | 已统一(现仅后端) |
| D10 | 谷歌路线 origin/destination 名称 vs 坐标 | 恒用坐标(停靠点已过有效性谓词) | 已统一(现仅后端) |

## 历史记录(统一前的主要分歧,2026-09-18 之前)

- 后端无坐标按"城市名含汉字"判国内 → 巴厘岛/东京等汉字名海外地拿到错误国家的高德链接;
- 0/0 哨兵在后端判定阶段(当真坐标→误判海外)与打点阶段(当无坐标)两处口径相反;
- 越界坐标(200, -95)后端不校验,国界框落空误判海外,路线中照常纳入;
- 高德路线 via 无上限、谷歌路线 waypoints 无上限,高德坐标不换算 GCJ-02;
- (2026-10-04 前)前端 Vue 版 `geo.ts` 曾有完整搜索/路线深链,与后端以本表+共享 case 双向钉住;
  React 迁移后该实现退役,由 R1/R3/R4 记录机制性差异;
- (2026-10-04 当日)原 R2"前端无有效性谓词,哨兵/越界坐标照常出链接"经用户拍板补齐消解:
  前端 `mapPins.hasValidCoords` 与后端 `_parse_coords` 同语义,哨兵/越界视同无坐标(不进 pin、
  计入脚注隐藏数),`search_zero_sentinel`/`search_out_of_range_coords` 两侧期望同为"无出口"。
