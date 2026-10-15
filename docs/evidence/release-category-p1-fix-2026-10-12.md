# 类别排除 P1 修复与提示词／思考预算对照 — 2026-10-12

**类别证据错误放行的确定性漏洞已修复。提示词与思考预算完成了 13 次真实调用对照，但尚不能据此宣布严格饮食行程可稳定交付，发布结论仍是 NO。** 本轮仅修改类别规则及相应用例，实验没有修改正式提示词、环境文件或生成阶段的思考开关。

接续[当前状态复核](current-state-review-2026-10-12.md)。旧证据保留。本轮起始 HEAD 为 `1d67c44`；期间已有分支合并使 HEAD 推进为 `367a68d`，最终门禁以其工作树为准。本轮保留其余未提交修改，未创建提交或部署。

## 类别规则修复

根因是把“标签没有命中寺庙”直接当作“有证据证明不是寺庙”，并仅解析类别字段键，不检查字段证据的资格。`浅草寺 + amenity=place_of_worship` 缺少 religion 时，以及名称含神宫但类别字段未验证／stale 时，都错误变成 pass。

[category_evidence.py](../../travel-agent-python/app/agent/generation/rules/category_evidence.py)现在集中定义证据资格和三态判定，由原有 [constraint_checks.py](../../travel-agent-python/app/agent/generation/rules/constraint_checks.py)直接消费，没有保留旧 helper 包装层。

- 类别字段及 identity 都须为 verified / observed，不能标记 stale；provider 和实体 sourceUrl 必须存在且彼此一致。
- 明确寺庙／神社的宗教、历史或建筑标签优先阻断；冲突的公园／商店标签不能掩盖它。
- 礼拜场所缺 religion、泛泛的 tourism=attraction 等返回“证据不足”，保留既有保守名称判据。
- 可信的独立公园、商店、餐饮等用途，或明确属于其它已支持宗教的礼拜场所，仍可解除名称误判。没有把所有带“神宫”的园区永久禁掉。
- 每个条目独立检查，同名条目不能借另一个条目的公园证据放行。

新增回归先在原代码上得到 **24 failed / 5 passed**，再验证修复。最终类别专用矩阵为 35 项；与既有证据票链路合跑 **67 passed**。相邻内容要求与交付边界组合复验 98 项通过，随后补充的建筑冲突用例另已通过。生产签票→字段证据→类别检查的公园正向链路也已锁定，见[类别用例](release-category-p1-fix-2026-10-12/category-tests.log)、[修复前失败](release-category-p1-fix-2026-10-12/before-tests.log)和[原问题回放](release-category-p1-fix-2026-10-12/rule-replay.log)。

最终[完整门禁](release-category-p1-fix-2026-10-12/final-gate.log) **ALL GREEN：1847 passed、1 skipped、3 warnings，809.97 秒**。diff-cover 对已跟踪的 102 行改动为 100%；它不统计未跟踪新文件，故另查 coverage.xml：新类别模块为 **39/40 行（97.5%）**，未覆盖的是调用方已过滤受控类别后的防御性分支。四套行为评测已 exit 0，31 项[棘轮](release-category-p1-fix-2026-10-12/eval-ratchet.log)在原基线内。未改 schemas、迁移、前端或门禁上限。

## 对“提示词僵化／提高思考强度”的实际检查

当前阻塞整趟、整趟流式、单日和 JSON 修复入口显式传入 `enable_thinking=False`。因此只把环境的 `LLM_REASONING_EFFORT` 调高，生成调用仍关闭思考。这是代码事实，不是根据结果猜测。

DeepSeek 当前官方参数说明把 `medium` 映射为 `high`，不是两个独立档位，见[官方 Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)。本轮确实分别请求了 medium 与 high；使用同一已配置 `deepseek-flash`、温度、JSON 输出形状、原始东京需求、同一档内冻结资料，保存最终回答和 usage，没有保存思考正文。

实验只对比首答生成及引用落地／去重／规则校验：不做重新研究、二次地理编码或图内重试，未经过完整业务落库与浏览器创建。因此这里的“规则通过”不是应用完成率，也不证明实时菜单、营业时间或无障碍事实正确。

三组资料／预算的结果分别看，不能混算：

| 冻结资料 | 提示词／思考 | 预算 | 完整 JSON | 现有规则通过 | 用时 |
|---|---|---:|---:|---:|---|
| 历史 21 号观测行，只有两家有饮食依据的餐饮实体，且地理分散 | 当前／关闭 | 16000 | 2/2 | 0/2 | 9.42–12.75s |
| 同上 | 当前／medium 或 high | 16000 | 0/2 | 0/2 | 96.78–101.05s |
| 同上 | 精简／high | 16000 | 2/2 | 0/2 | 56.56–84.95s |
| 同上，直接复验先前截断场景 | 当前／high | 32000 | 1/1 | 0/1 | 93.08s |
| 六个既有真实 Overpass 餐饮实体观测，含 restaurant 与 food_court | 当前／关闭 | 16000 | 1/1 | 0/1 | 10.05s |
| 同上 | 当前／high | 16000 | 1/1 | 1/1 | 65.66s |
| 同上 | 精简／关闭 | 16000 | 1/1 | 0/1 | 8.58s |
| 同上 | 精简／high | 16000 | 1/1 | 0/1 | 48.61s |
| 同上 | 当前／high | 32000 | 1/1 | 0/1 | 76.17s |
| 同上 | 精简／high | 32000 | 1/1 | 0/1 | 63.02s |

精简版本复用原请求动态追加的需求、参考资料、价格口径与输出字段，整理优先级、删除重复叙事示例，并强调沿用参考资料正式名、资料不足不得虚报。这是一个候选版本；没有做历史 prompt 版本的同口径纵向对照，不能把结果说成“多次迭代已导致质量下降”的因果证明。

首组资料下，medium 样本的 **15831 / 16000** completion tokens 用于 reasoning，另一个 high 样本为 14944，均 finish_reason=length、JSON 截断。最后把这个同提示词、同资料场景直接调到 32000：finish_reason=stop，完整 JSON，实际消耗 **16417 completion tokens，其中 14141 reasoning tokens**，超过了原 16000 上限。用户关于思考占用输出预算的判断有直接观测支持；该例仍因转场和后处理缺餐不合格。换成六个餐饮实体后，high 在 16000 内也能输出完整结果；32000 两例仍有实质约束问题，因此没有证据证明“调大预算即可稳定改善”。每个较充分资料分组只有一个样本，不能据此估计总体成功率或宣称精简版优于当前版。

原始记录：[资料较少组](release-category-p1-fix-2026-10-12/v2/summary.json)、[六实体组](release-category-p1-fix-2026-10-12/adequate/summary.json)、[32000 组](release-category-p1-fix-2026-10-12/wide/summary.json)、[截断场景直接提高预算](release-category-p1-fix-2026-10-12/wide-scarce/sample-0.json)。各目录的 setup 与 sample JSON 保存输入、提示词指纹、最终回答、usage 和落地结果。

## 生成之后发生的变化

不能只看展示结果判断模型是否漏餐。[后处理回放](release-category-p1-fix-2026-10-12/postprocess.json)显示，十一个可解析首答都原本给出了四餐。后续引用类型覆盖和去重使多个结果只保留两餐或三餐。

六实体组的当前／high 样本原始输出为第 1 天 PLANT BASED、AFURI；第 2 天 FOOD HALL BLAST、Saishoku kenbi。落地后 FOOD HALL BLAST（OSM way 232497186）因与 PLANT BASED（OSM node 12060629549）距离 **5.09 米**，被统一同类型 80 米判重删除；没有补餐，结果仍通过现有规则（规则目前未强制全天一午一晚）。

两者可能是食品厅与内部店铺的关系，当前证据不支持直接断言为两家完全独立餐馆。已确认的是：去重改变了原始完整餐饮安排，且未补齐或显式阻断缺餐。其它样本还出现餐饮被参考池 category 覆盖为 attraction，饮食检查因只看 food 而漏项。这些问题应与 prompt 分开修复。

32000 当前／high 样本保留四餐，但“菜食健美”在本轮冻结资料落地中未与 `Saishoku kenbi` 建立匹配，且新宿御苑到 AFURI 仅留 15 分钟，坐标规则要求约 34 分钟。实验没有做二次名称解析，不能据此判定该餐馆不存在。精简／high 32000 样本也被去重删餐，且有转场与活动时长不足。

## 下一步

类别错误放行已按本轮边界修复，其它 P1 保留。结果支持继续按生成阶段设置实际思考开关及输出预算，同时整理提示词的优先级、正式名称／证据引用规则；先修复后处理删餐与类型覆盖，再比较最终交付质量，避免误判 prompt 的效果。正式参数应经更多城市、重复样本与完整业务链验收后确定。

初版实验脚本误把 `chat()` 的字符串返回值按结构响应读取，已停止并改用 `chat_response()`；该批 harness 错误记录没有纳入以上 13 个样本，也没有记为产品失败。
