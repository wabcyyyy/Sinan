# 饮食证据链贯通与多日重试 — 2026-10-12

**本轮把"外部饮食标签"第一次真正送进了交付结果：严格模式样本 21 的两餐都带上了 `diet:*` 字段证据，其中 `くら寿司` 锚定到 OSM node 1174641596（与实时探针同一实体），上一轮它落在 way 561002505 且标签为零。但严格模式 4 次真实尝试仍 0 次完成，公开上线结论仍为 NOT READY / NO。**

本文接续[内容约束 P1 修复与复验](release-content-p1-fix-2026-10-11.md)、[多日交付修复](release-p1-fix-2026-10-11.md)与[首轮上线验收](release-audit-2026-10-11.md)。旧文档与旧失败证据原样保留，本文只评价此次变更；文中门禁数字以本文为准，取代旧文档中的对应值。

## 接手时的实际状态

上一轮交接说"1768 passed、变更行覆盖率 100%、四套评测与 31 项棘轮通过"。**当场核验并不成立**：离线全量为 21 failed / 1770 passed，`check.ps1` 在 pyright 阶段红，规模棘轮超限，四套评测与棘轮的真实退出码未经确认。本轮先修复这些，再谈剩余 P1。

| 接手时的真实故障 | 根因 | 处置 |
|---|---|---|
| 89 个用例在 fixture 阶段 ERROR（连带 21 failed） | Overpass 客户端已拆到 `data/overpass.py`（名为 `_client`），`tests/conftest.py` 仍引用 `places._overpass_client`；新增回归文件同样引用 `places.search_diet_tagged_places`、并把 fast 模型助手挂在 `existence` 上（实际在 `grounding/core_name.py`） | 全部改指真正持有该符号的模块；不新建兼容 re-export（AGENTS.md 哲学 4） |
| pyright 3 个新错 | `overpass._normalize` 用三元式内联 `isinstance`，`tags` 仍是 `\| None` | 改为 `places._osm_facts` 已有的局部变量收窄写法 |
| 规模棘轮 34/33、13/11 | 新代码把 `field_fact_evidence` 推到 78 行，`day_stream.py`/`config.py` 越过 400 行 | 抽出 `_external_tag_evidence`；复用同域既有 `ctx` 局部；配置项改行尾注释。**棘轮上限一字未改** |

## 修复及边界

| 原问题与根因 | 此次修改 | 验证 |
|---|---|---|
| 严格模式在源头就拿不到饮食证据：`search_foods` 把 Overpass diet 池行**原样合并**，而 OTM/web 池都经 `_as_candidate` 当场签票。落地只认证据票、不认行上的 `source` 字符串 | Overpass 行同样走 `_as_candidate` 入池 | 新增回归锁定"入池即签票"；真实样本 21 两餐首次带 `diet:*` 交付 |
| 按名解析路径同源断供：`get_poi_detail` 只搬 name/坐标/地址/source，把 `hit` 上已由 `_osm_facts` 填好的 `dietary_options`/`category_tags`/`source_url` 丢掉，当场签出的票没有饮食依据 | 三个证据字段随票带走 | 回归锁定票含 `diet`、`category` 与实体链接 |
| 去重/否证清空当日直接落成失败天：反馈文案早已写明"已移除哪些名字、改选其他片区"，但整段多日链 `max_attempts=1`，这条反馈生成后即被丢弃 | 复用 `BUDGET_OVERAGE_MARK` 同口径新增 `DEDUP_EMPTIED_MARK`，据此破例给一次反馈重试，仍收敛到 fallback | 路由回归；**离线评测从不触发该分支，故此修复仅有单元覆盖**（见下"验证盲区"） |
| 排除类别误伤（上一轮遗留观察） | 未改判据；证据双向判定的代码在上一轮已落，本轮补上此前未覆盖的分支用例 | 变更行覆盖 272/273 |
| 离线评测自身掩盖缺陷 | `eval_agent.py` 用只接 2 参数的 lambda 打桩 `search_foods`，接到新 `dietary` 关键字后抛 TypeError，被"research agent food failed"**静默吞掉 13 次**，整个 food 研究域在评测里消失，而评测仍 exit 0 | 迁移打桩签名；`attraction_duplicate_rate`、`terminal_reset_day_rate`、`research_pack_avg`、`research_rounds_avg` 四项随即回到基线 |

## 真实模型复验（隔离栈：MySQL 3307 / Redis 6380 / 后端 8001 / 预览 5174）

输入与上一轮同一句话：东京 2 天、2 人、3000 元、70 岁老人、慢节奏、不去寺庙、每天最多两个景点、素食可吃蛋奶；先过真实 `/clarify`，不手工补字段。

| 样本 | 代码版本 | 实际结果 | 用时 | 逐日餐饮证据 |
|---|---|---|---:|---|
| 候选 id=16 | 修复前 | COMPLETED / READY_WITH_WARNINGS，**首次即完成**（上一轮需显式重试） | 53.25s | 5 项中 0 餐有据 |
| 严格 id=17 | 修复前 | FAILED / BLOCKED | 67.39s | 第 2 天 `おきなわ軒` 拿到 `diet:vegetarian`（全链路首次） |
| 严格 id=18 | 修复前 | FAILED / BLOCKED | 55.98s | 4 餐全部 0 据（`くら寿司` 落到 way 561002505） |
| 浏览器会话创建 id=19 | 修复前 | day1 SUCCEEDED / day2 FAILED | — | day2 因"去重后无可游玩景点"清空 |
| 候选 id=20 | 修复后 | COMPLETED / READY_WITH_WARNINGS | 43.75s | 4 餐全部 0 据，且 `poiId` 全为 None（food 研究超时所致） |
| 严格 id=21 | 修复后 | FAILED / BLOCKED | 63.78s | **2 餐各有据**：`Délifrance` = node 1511441588 `diet:vegetarian`；`くら寿司` = node 1174641596 `diet:vegan`+`vegetarian` |

21 号的两个 `osm:node` 与[实时 Overpass 探针](release-diet-evidence-2026-10-12/overpass-live-probe.json)返回的实体一致，因此"池→票→字段证据→交付"这条链已被外部数据独立佐证。

但 21 号仍失败，且**失败原因已不是证据管道**：DB 逐日阻断原文见 [blockers-from-db.json](release-diet-evidence-2026-10-12/blockers-from-db.json)，day-1 写的是「**Délifrance 新宿店**」尚无依据，而交付项名为 `Délifrance` 且已带 `diet:vegetarian`——模型写的分店后缀名与锚定后的权威名，在交付检查的时点上不是同一个键。这是下一个待确认根因（怀疑证据字段挂载晚于该次判定），本轮未改。

严格模式累计 5 次真实尝试（12、15、17、18、21）0 次完成。结论只能写成：严格模式**不再结构性不可能**，但**仍不可靠**。

## 本轮新发现的缺陷（未修，需决策）

`search_foods` 的工具预算是默认 **10s**（`tools/registry/core.py:116` 按 `spec.timeout_seconds` 截断），而 Overpass 客户端配置为 `timeout=25s` + `retry_attempts=1`，最坏约 2×25+1.1s。**只要 Overpass 慢于 10s，失败的不是"没拿到饮食标签"，而是整个 food 研究域**：03:32:57 真实日志 `research agent food failed: 工具 search_foods 超时（>10s）`，随后 20 号候选四餐 `poiId` 全 None、零证据。压缩超时会大量丢失证据，放宽工具预算会拖慢研究——这是延迟与证据覆盖的取舍，属产品决策，本轮不擅自定值。

## 验证盲区（必须与绿灯一起读）

1. 离线套件把 Overpass/Nominatim 等外呼钉死为关闭，因此 1801 个用例**结构上无法**发现上面的超时不匹配。
2. `tests/agent_eval/mock_llm.py` 给每个 fixture 池行统一签发证据票，等于替生产补做了入池签票——这正是"入池不签票"缺陷能长期隐身的原因。已补的用例锁定生产口径，但 mock 的这层美化本身值得后续收严。
3. 去重重试修复只有路由单测覆盖，评测指标一字未动，因此其真实效果仅在真实模型上可见。
4. 本轮**没有浏览器像素级验证**：应用内浏览器无可用视口（viewport 0×0 / `visibilityState=hidden`），指针与截图动作不可执行。已完成的只是**结构型证据**——在同源页面内注册合成账号 `p1fix2_browser_qa`、经 HttpOnly Cookie 建立真实会话、创建行程 19 并读回逐日状态。这证明会话与数据面贯通，不证明界面渲染与告警可见，不能替代上一轮的截图验收。
5. 外部服务抖动真实存在：本轮观察到 Nominatim `SSL: UNEXPECTED_EOF` 连串失败、Overpass 一次 504（清缓存后重试即成功 8 行）。负缓存 TTL 600s 意味着一次 504 会让后续 10 分钟内的严格模式样本零证据。
6. 一次 `/clarify` 对同一句话返回全空槽位，紧邻 3 次重跑均稳定抽出 `maxAttractionsPerDay=2`、`dietaryRequirement=vegetarian`、`temple` 排除，见 [clarify-extraction-variance.json](release-diet-evidence-2026-10-12/clarify-extraction-variance.json)：偶发而非系统性的抽取不稳。

## 环境故障曾伪装成产品故障

3307 端口 TCP 可 accept 但**握手 0 字节**——隔离 MySQL 容器 02:56 重启后 Docker 端口转发成僵尸，后端每次登录 HTTP 500「系统繁忙」。容器内 MySQL 自身正常。恢复动作：先 `mysqldump` 备份（`release_audit_20261011`，19 张表）再 `docker restart ta-mysql`，重启后端清掉死连接池，登录恢复 200。记录在此是为了提醒：这类噪声极易被误读为交付链路回归。

## 工程验证

| 检查 | 结果 |
|---|---|
| `scripts/check.ps1` 全门禁（最终树） | **ALL GREEN**；1801 passed、1 skipped、3 既有 warnings |
| 变更行覆盖率 | 272 / 273 行 = 99%（唯一未覆盖是 `constraint_checks.py:107`：`_category_matches_exclusion` 的兜底 return，调用方已过滤类别，**该分支不可达**） |
| 静态与边界 | ruff check/format 通过；pyright 0 错；import-linter 通过；密钥扫描通过；契约漂移通过（本轮未改 schemas） |
| 规模棘轮 | 长函数 33/33、超长文件 11/11，**上限未放宽** |
| 四套离线评测 | eval_agent / eval_research / eval_editing / eval_experience 均 exit 0 |
| 评测棘轮 | 31 项指标在基线内，未使用 `--update`，未放宽任何界值；`attraction_duplicate_rate` 0.0、`terminal_reset_day_rate` 0.0154、`research_pack_avg` 18、`research_rounds_avg` 3 |
| 前端 | production build 通过；`test:unit` 306（vitest）+ 5（delivery-gate）通过；theme/ep 门禁通过；本轮未改前端代码 |
| 新增回归 | 10 项（入池签票、按名解析随票证据、核心名重查三态、类别/空名/无 provider 兜底、饮食标注入参考资料、去重重试路由） |

未提交任何 commit，未发布应用，未改动迁移。隔离栈的合成账号与行程 16–21 留在 `release_audit_20261011` 以便复核。

## 仍未闭环的发布风险

1. **P1：严格饮食模式不可靠。** 管道已通，但 5 次真实尝试 0 次完成；剩余已知问题是分店后缀名与权威名在判定时的键不一致，以及 `search_foods` 超时预算与 Overpass 时延不匹配。
2. **P1：复杂多日一次完成率。** 候选模式本轮两次首答即完成（16、20），去重重试修复已落但只由单测保证；尚无"清空日后能补齐"的真实样本证据。
3. **浏览器像素级验收缺失**（见验证盲区 4）：需在应用内浏览器可见后补做，否则界面告警可见性只沿用上一轮结论。
4. **预算与其余旅行事实仍为估算**；本轮成功样本在预算内不构成票价、汇率、无障碍路线或菜单保证。首轮报告其他未修条目继续适用。
5. 未做整套安全红队与全设备矩阵，本次局部复验不替代首轮完整验收。

**若今天公开给真实用户，仍建议 NO。**

证据：[候选 20](release-diet-evidence-2026-10-12/live-candidate-trip20.json)、[严格 21](release-diet-evidence-2026-10-12/live-strict-trip21.json)、[候选 16](release-diet-evidence-2026-10-12/live-candidate-trip16.json)、[严格 17](release-diet-evidence-2026-10-12/live-strict-trip17.json)、[严格 18](release-diet-evidence-2026-10-12/live-strict-trip18.json)、[逐日阻断原文](release-diet-evidence-2026-10-12/blockers-from-db.json)、[Overpass 实时探针](release-diet-evidence-2026-10-12/overpass-live-probe.json)、[clarify 抽取波动](release-diet-evidence-2026-10-12/clarify-extraction-variance.json)。
