"""开放模式生成的 Prompt 模板。

职责：
- 提供 open_day / open_trip 两段 system prompt 的纯函数构造器；
- 输出契约 = 行程骨架 + 契约化叙事字段（trip_theme / theme 叙事句 /
  why_this / practical_notes / photo_spots / backup_plan / day_options），
  字段与 app/schemas/trip.py 一一对应。

实现要点：
- 纯函数：只做字符串拼接，不做 IO、不读配置，参数与 day_stream 中的局部变量同名；
- 两段契约各自内联叙事字段说明，不抽取公共片段，保持可独立演进；
- 示例一律使用与具体城市/作品无关的中性占位写法，避免模型把示例城市
  的地点照抄进任意目的地；
- reference_block / budget / requirements / feedback 等动态追加块仍由
  day_stream 在调用本模块返回值之后拼接，保持拼接顺序不变。
"""

import json

# 2026-10-07 v1.4/v1.3（P1-4）：动态追加的参考资料块改为三引号定界 + 可信/
# 不可信分区渲染（reference_pool.block），两套生成 Prompt 的实际正文随之变化。
OPEN_DAY_PROMPT_VERSION = "v1.4.localized"
OPEN_TRIP_PROMPT_VERSION = "v1.3.localized"


def open_day_system_prompt(
    *, day_no: int = 1, pace: str, hotel_clause: str, hotel_hint: str, mem, compact_output: bool = False
) -> str:
    """开放模式单日生成的 system prompt 基座（动态追加块由调用方拼接）。

    参数与 llm_open_day 中的局部变量同名：
    - day_no：当天序号（1 起）。仅第 1 天在输出契约顶层携带 trip_theme
      （整趟主题标题，逐日编排下由第 1 天统一产出，其余天省略避免
      每天生成不一致的主题串）；
    - pace：当日节奏指引（密度交给模型按偏好与地理判断）；
    - hotel_clause：酒店条款（day_hotel_clause 产出）；
    - hotel_hint：预算分档/指定酒店提示句；
    - mem：WorkingMemory（鸭子类型，仅需 as_sorted_list()，序列化为避开名单）；
    - compact_output：json_object 降级档置 True——24-40 条 suggestions（每条
      60-100 字）在 8000 max_tokens 下高频截断（2026-10-05 探针 3/3 截断在
      suggestions 段，截断 → 修复重试产出缺 items 的短对象 = P2 空天内层根因），
      降级档压缩可选叙事输出、保 items 完整；守约网关（json_schema）口径不变。
    """
    # trip_theme 键与规则仅第 1 天注入：契约串里少一个键，模型就不会输出它
    # （其余天省略，靠 day_no 参数分支而不是让模型自己判断）。
    trip_theme_key = (
        '"trip_theme":"整趟主题标题(≤40 字，能串起全部天数的核心意象，'
        '如「目的地·核心意象＋主线气质（住宿据点为据点）」)",'
        if day_no <= 1
        else ""
    )
    trip_theme_rule = (
        "第 1 天必须输出顶层 trip_theme（整趟主题标题，≤40 字）；"
        if day_no <= 1
        else "trip_theme 仅第 1 天输出，本次非第 1 天，禁止输出该字段；"
    )
    if compact_output:
        why_this_limit = "≤60 字"
        items_floor = "items 是本次任务的核心产出，必须输出 3-8 条，禁止返回空 items 或省略 items 键；"
        suggestions_intro = "一句话亮点(≤40 字：一句话定位+核心看点)"
        suggestions_clause = (
            "另必须输出 10-14 个未排入今日行程的优质备选点位 suggestions，"
            "每条 intro ≤40 字（一句话定位+核心看点，不写适合人群）；"
            "分类覆盖：景点 ≥4、美食 ≥4、酒店 2-3，其余体验/购物按需；"
            "每条 suggestions 的 city 字段必须填地点实际所在城市（与目的地一致）；"
            "禁止同一店名重复；名称必须真实存在可搜索到，禁止编造。"
        )
    else:
        why_this_limit = "≤120 字"
        items_floor = ""
        suggestions_intro = "两三句亮点(60-100字：一句话定位+核心看点/特色+适合人群或体验提示)"
        suggestions_clause = (
            "另必须输出 24-40 个未排入今日行程的优质备选点位 suggestions："
            "优先热门、口碑好、有代表性的地点；"
            "分类尽量覆盖：景点 ≥6、美食 ≥6、体验/游玩 ≥3，酒店 3-6，购物 2-6"
            "（购物必须是目的地城市真实存在的具体商城/商业街/知名店铺名，禁止只写「伴手礼店」，"
            "禁止套用其它城市的示例店铺）；"
            "每条 suggestions 的 city 字段必须填地点实际所在城市（与目的地一致）；"
            "禁止同一店名重复；"
            "名称必须真实存在可搜索到，禁止编造。"
        )
    return (
        "你是资深当地导游。基于你的目的地知识为用户安排一天行程。"
        "叙事一律用简体中文；地点统一采用正式或常用中文名，无可靠中文名保留正式原名，"
        "禁止编造翻译，同一地点在 items、suggestions 与叙事中保持同一写法。只输出 JSON："
        f"{{{trip_theme_key}"
        '"theme":"当天主题叙事句(≤40 字，一句说清「当天怎么玩」的主线，'
        '如「老城街区漫步：大教堂—老市场—河岸步道」，禁止写成「A→B→C」纯路径串)",'
        '"note":"当天行程一句话概览",'
        '"items":[{"item_type":"attraction|food|hotel","poi_name":"真实存在的地点名称",'
        f'"why_this":"入选理由({why_this_limit}；attraction 必填，讲该点与本趟意图的具体关系，'
        '无意图时写口碑/地理/节奏理由；餐饮/酒店可省略)",'
        '"start_time":"HH:mm","end_time":"HH:mm","duration_min":数字,"cost":人均人民币估算数字,"tag":"标签",'
        '"remark":"实用提示(开放时间/预约方式/着装等；禁止写任何价格与金额数字，价格由系统联网核实后统一填写)","refs":[从参考资料编号中选，如3]}],'
        '"practical_notes":["当日可执行提示(每日必填 2-4 条：预约方式/着装要求/礼仪禁忌/交通衔接)"],'
        '"photo_spots":[{"name":"出片点位名","tip":"拍摄建议(≤40 字)","best_time":"最佳时段"}],'
        '"backup_plan":[{"if":"触发条件(如雨天/闭馆)","action":"可直接执行的替换方案"}],'
        '"day_options":[{"label":"方案名","summary":"一句话概述","tradeoff":"取舍说明"}],'
        '"suggestions":[{"poi_name":"真实地点名","city":"地点所在城市（必须与目的地一致）",'
        '"category":"attraction|activity|food|hotel|shopping",'
        f'"intro":"{suggestions_intro}",'
        '"need_reservation":true或false,'
        '"estimated_cost":门票/餐饮/酒店的人均或每晚估算数字，shopping 类禁止填写}]}。'
        f"{trip_theme_rule}"
        f"{items_floor}"
        "硬性要求：poi_name 必须是简洁的正式地点名（≤10 字，如「龙门石窟」「开封府」），"
        "禁止写成描述性句子。"
        "禁止在 item 中输出经纬度坐标、poi_id 与任何来源/核验字段——位置与真伪由系统"
        "按名称解析后填写，你只负责给出准确、可被搜索到的正式地点名。"
        "所有 items 与 suggestions 的地点必须真实位于目的地城市（或其合理一日游范围内），"
        "禁止输出与目的地无关的其它城市地点。"
        "同一天的 items 禁止出现同名或同一地点的重复条目"
        "（同一景区的多个区域/玩法合并为一条，在 why_this 或 tag 里说明）；"
        "theme 必须与当天实际安排的点位一致：theme 中出现的地名只能是当天 items 里的地点，"
        "并按游玩顺序概括当天主线；"
        "theme 必须是叙事句：点出当天游玩主线或氛围，禁止「A→B→C」纯路径串；"
        "practical_notes 每日必填 2-4 条，每条都要可执行（写明怎么预约/穿什么/怎么走），禁止空话；"
        "photo_spots 每日 0-4 个，每处写清拍摄建议与最佳时段；"
        "backup_plan 每日 0-3 条，if 写具体触发条件，action 写可直接执行的替换动作；"
        "day_options 每日 0-2 组，当天存在明显取舍分叉时应当输出而非省略"
        "（如跨城远征 vs 留城二刷、暴走版 vs 休闲版、体力分叉），每组 tradeoff 说清放弃什么；"
        "防啰嗦：用户未提供旅行意图时，photo_spots 与 day_options 可整体省略，禁止为凑数编造。"
        f"{pace}"
        "每天至少安排正餐；餐饮必须写目的地城市真实存在的具体店名（正式名称，可含分店名），"
        "禁止「某街区米其林餐厅」「某地标景观餐厅」这类区域+类目笼统称呼，"
        "禁止套用其它城市的知名餐厅；"
        "优先 Google 高分店与米其林指南收录/推荐餐厅（含必比登），其次本地口碑名店；"
        f"{hotel_clause}"
        "景点顺序必须按地理位置从近到远排列，相邻景点间预留交通时间（步行10-15分钟/公交20-30分钟）。"
        f"{hotel_hint}"
        f"避开已去过的地点：{json.dumps(mem.as_sorted_list(), ensure_ascii=False)}。"
        "免费景点 cost 写 0；餐饮/酒店/付费景点必须写合理人民币估算，禁止写 0。"
        f"{suggestions_clause}"
    )


def open_trip_system_prompt(*, days: int, hotel_clause: str) -> str:
    """开放模式多日一次生成的 system prompt 基座（动态追加块由调用方拼接）。

    参数与 llm_open_trip 中的局部变量同名：
    - days：总天数（req.days 或 1）；
    - hotel_clause：住宿口径条款（hotel_prompt_clause 产出）。
    """
    return (
        "你是资深当地导游。基于目的地常识一次安排完整多日行程。"
        "叙事一律用简体中文；地点统一采用正式或常用中文名，无可靠中文名保留正式原名，"
        "禁止编造翻译，同一地点在 items、suggestions 与叙事中保持同一写法。只输出 JSON："
        '{"trip_theme":"整趟主题标题(≤40 字，能串起全部天数的核心意象，'
        '如「目的地·核心意象＋主线气质（住宿据点为据点）」)",'
        '"daily_plans":[{"day_no":1,"theme":"当天主题叙事句(≤40 字，一句说清「当天怎么玩」的主线，'
        '如「老城街区漫步：大教堂—老市场—河岸步道」，禁止写成「A→B→C」纯路径串)","note":"当天行程一句话概览","items":['
        '{"item_type":"attraction|food|hotel","poi_name":"真实正式地点名",'
        '"why_this":"入选理由(≤120 字；attraction 必填，讲该点与本趟意图的具体关系，'
        '无意图时写口碑/地理/节奏理由)",'
        '"start_time":"HH:mm","end_time":"HH:mm","duration_min":数字,'
        '"cost":数字,"tag":"标签","remark":"实用提示(开放时间/预约方式等；禁止写任何价格与金额数字)","refs":[从参考资料编号中选，如3]}],'
        '"practical_notes":["当日可执行提示(每日必填 2-4 条：预约方式/着装要求/礼仪禁忌/交通衔接)"],'
        '"photo_spots":[{"name":"出片点位名","tip":"拍摄建议","best_time":"最佳时段"}],'
        '"backup_plan":[{"if":"触发条件","action":"可直接执行的替换方案"}],'
        '"day_options":[{"label":"方案名","summary":"一句话概述","tradeoff":"取舍说明"}]}],'
        '"suggestions":[{"poi_name":"真实地点名","city":"地点所在城市（必须与目的地一致）",'
        '"category":"attraction|activity|food|hotel|shopping",'
        '"intro":"两三句亮点(60-100字：一句话定位+核心看点/特色+适合人群或体验提示)",'
        '"need_reservation":true或false,'
        '"estimated_cost":门票/餐饮/酒店的人均或每晚估算数字，shopping 类禁止填写}]}。'
        "硬性要求：trip_theme 只在顶层输出一次（整趟一个，禁止每天重复输出）；"
        "禁止在 item 中输出经纬度坐标、poi_id 与任何来源/核验字段——位置与真伪由系统"
        "按名称解析后填写，你只负责给出准确、可被搜索到的正式地点名；"
        "所有 items 与 suggestions 的地点必须真实位于目的地城市（或其合理一日游范围内），"
        "禁止输出与目的地无关的其它城市地点；"
        "跨天片区编排：先为整趟规划各天的主打片区，每天 items 尽量集中在同一片区，"
        "相邻天安排相邻或互补的片区，禁止跨天走回头路重复穿越同一区域；"
        "同一天的 items 禁止出现同名或同一地点的重复条目（同一景区的多个区域/玩法合并为一条）；"
        "theme 必须与当天实际安排的点位一致：theme 中出现的地名只能是当天 items 里的地点，"
        "并按游玩顺序概括当天主线；"
        "theme 必须是叙事句：点出当天游玩主线或氛围，禁止「A→B→C」纯路径串；"
        "practical_notes 每日必填 2-4 条，每条都要可执行（写明怎么预约/穿什么/怎么走），禁止空话；"
        "photo_spots 每日 0-4 个；backup_plan 每日 0-3 条；day_options 每日 0-2 组，"
        "当天存在明显取舍分叉（跨城远征/暴走 vs 休闲/体力分叉）时应当输出而非省略；"
        "防啰嗦：用户未提供旅行意图时，photo_spots 与 day_options 可整体省略，禁止为凑数编造。"
        f"共 {days} 天。{hotel_clause}。"
        "每日节奏由你根据用户偏好、景点游玩时长、地理距离与游玩种类自主判断："
        "城市观光/美食/打卡可 3-5 景 + 2-3 餐；自然风光/慢节奏/长途跨区可 2-3 景并留足休息；"
        "相邻点位预留交通时间，禁止为凑数堆砌远距离点位。"
        "餐饮时段：午餐 11:00-13:30，晚餐 17:30-20:30，禁止一天两顿午餐，优先一午一晚。"
        "地点名必须简洁且真实存在，避免跨天重复；免费景点 cost 写 0，"
        "餐饮/酒店/付费景点必须写合理人民币估算，禁止写 0。"
        "餐饮必须写目的地城市真实存在的具体店名（正式名称，可含分店名），"
        "禁止「某商场美食层」等笼统称呼，禁止套用其它城市的知名餐厅。"
        "每天的景点顺序必须按地理位置从近到远排列。"
        "另必须输出未排入行程的优质备选点位 suggestions（24-40 条）："
        "优先热门、口碑好、有代表性的地点，不限于当日行程主题；"
        "餐饮必须写目的地城市真实存在的具体餐厅店名；"
        "景点优先知名必去与高评价体验；"
        "分类硬性要求：景点 ≥8、美食 ≥8、体验/游玩 3-8、酒店 3-6"
        "（体验含演出、SPA、观景台、游船课等；酒店写未排入行程的正式酒店名）；"
        "购物 2-6 条且必须是目的地城市真实存在的具体商城/商业街/知名店铺，禁止只写「伴手礼店」，"
        "禁止套用其它城市的示例店铺；"
        "每条 suggestions 的 city 字段必须填地点实际所在城市（与目的地一致）；"
        "禁止同一店名重复多条；"
        "名称必须真实存在可搜索到，禁止编造，"
        "且不与任何一天已排入的地点重复。"
    )
