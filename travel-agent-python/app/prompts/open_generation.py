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

# 2026-10-07 v1.5/v1.4：把终检的单日时长窗口（MIN_ACTIVE_MINUTES/MAX_DAILY_MINUTES）
# 与景点上限写进两套 Prompt——此前 prompt 说「密度你看着办」而门禁按硬窗口判，
# 实测 68 天里 20 天因过满/过稀被重置（详见各构造器 docstring）。
# 2026-10-07 v1.4/v1.3（P1-4）：动态追加的参考资料块改为三引号定界 + 可信/
# 不可信分区渲染（reference_pool.block），两套生成 Prompt 的实际正文随之变化。
# 2026-10-12 v1.6/v1.7：条目新增 cost_currency（cost 数字的币种），系统按固定汇率确定性
# 换算成人民币，取代「按金额大小猜币种」（data.fx.normalize_item_cost）。
OPEN_DAY_PROMPT_VERSION = "v1.7.fact-caution"
# 2026-10-09 v1.5/v1.4（M5b，spec §11）：整趟主输出不再要求 suggestions 长备选数组
# （24-40 条 × 60-100 字是主 JSON 截断的头号税源）；备选池改由研究候选池确定性构建
# （open_plans.floor_suggestions / build_suggestions），缺口经既有有界富化补齐。
# 2026-10-12 v1.6（open_trip）：命名优先级（参考资料餐厅照抄店名）+ 删与结构化上限冲突的
# 密度示例 + 每天午晚餐各一 + 合并重复条款（见 open_trip_system_prompt docstring）。
OPEN_TRIP_PROMPT_VERSION = "v1.8.fact-caution"


def open_day_system_prompt(
    *,
    day_no: int = 1,
    pace: str,
    hotel_clause: str,
    hotel_hint: str,
    mem,
    min_active_minutes: int,
    max_daily_minutes: int,
    max_daily_attractions: int,
    compact_output: bool = False,
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
    - min_active_minutes / max_daily_minutes / max_daily_attractions：**终检判据**
      （reflect.validate_plans 的单日饱和度窗口与景点上限），由调用方从
      `generation.content.reflect` 取常量传入。刻意不在这里写死数字：本模块是
      纯字符串函数、不依赖 agent 层（反向 import 会与 day_prompts → 本模块成环），
      而自留一份 240/480 副本就是第二份真源，改一处漏一处。
      2026-10-07 实测：不告诉模型这条窗口时，68 天里 20 天因「过满 11 + 过稀 9」
      被终检重置——prompt 说「密度你看着办」、门禁按硬窗口判，模型无从命中。
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
        '"start_time":"HH:mm","end_time":"HH:mm","duration_min":数字,"cost":人均估算数字,'
        '"cost_currency":"cost 数字的币种代码(CNY 或目的地本币如 JPY)",'
        '"tag":"标签",'
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
        "practical_notes 每日必填 2-4 条，每条都要可执行（穿什么/怎么走/需核实什么），禁止空话；"
        "没有本次参考资料支持时，不得断言必须预约、免预约、开放时刻、闭馆日或免费寄存；"
        "这些信息写成出发前核实项，不得把模型记忆当作当前官方规定。"
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
        f"单日实质活动时长（当天景点与餐饮的停留时长之和，按 start_time 与 end_time 计）必须落在"
        f"{min_active_minutes}-{max_daily_minutes} 分钟：低于 {min_active_minutes} 分钟算安排过稀、"
        f"高于 {max_daily_minutes} 分钟算行程过满，两者都会被系统判为需要重排并退回重生成；"
        f"用户要慢节奏时取区间下沿，不要低于 {min_active_minutes} 分钟。"
        f"当天景点最多 {max_daily_attractions} 个（超过同样判过满）。"
        "餐饮时段：午餐 11:00-13:30、晚餐 17:30-20:30，禁止一天两顿午餐，优先一午一晚。"
        f"{hotel_hint}"
        f"避开已去过的地点：{json.dumps(mem.as_sorted_list(), ensure_ascii=False)}。"
        "免费景点 cost 写 0；餐饮/酒店/付费景点必须写合理估算并在 cost_currency 写明币种，禁止写 0。"
        f"{suggestions_clause}"
    )


def open_trip_system_prompt(*, days: int, hotel_clause: str, min_active_minutes: int, max_daily_minutes: int) -> str:
    """开放模式多日一次生成的 system prompt 基座（动态追加块由调用方拼接）。

    参数与 llm_open_trip 局部变量同名；min_active_minutes / max_daily_minutes 是单日
    实质活动时长的**终检窗口**（reflect.validate_plans），理由同 open_day_system_prompt。

    M5b（spec §11）：主输出契约不再包含 suggestions——长备选介绍（24-40 条 ×
    60-100 字）此前是整段 JSON 截断的头号税源（截断 → 修复重试 → 逐日兜底的
    失败链起点）。备选池由系统在研究候选池上确定性构建，模型不再产出。

    v1.6（2026-10-12，生成改走全局 low 思考后同输入 A/B）：只做四处可解释的清理——
    ①命名优先级：选用参考资料里的餐厅须逐字照抄资料店名（原「正式中文名 / 可含分店名」
    与引用规则冲突，模型把 Saishoku kenbi 译成「菜食健美」、给 AFURI 加「新宿店」，
    饮食依据挂不上）；景点仍用中文名（经存在性解析补坐标与类别证据）；②删去
    「3-5 景 + 2-3 餐」密度示例（与结构化景点上限冲突，实测排出 3 景）；③「优先一午一晚」
    改为每天午、晚餐各一；④合并重复的主题/顺路/跨天去重条款，删掉自身就是路径串的
    theme 示例。输出字段契约不变。
    """
    return (
        "你是资深当地导游。基于目的地常识一次安排完整多日行程。只输出 JSON："
        '{"trip_theme":"整趟主题标题(≤40 字，能串起全部天数的核心意象)",'
        '"daily_plans":[{"day_no":1,"theme":"当天主题叙事句(≤40 字，一句说清「当天怎么玩」的主线)",'
        '"note":"当天行程一句话概览","items":['
        '{"item_type":"attraction|food|hotel","poi_name":"地点名（命名规则见下）",'
        '"why_this":"入选理由(≤120 字；attraction 必填，讲该点与本趟意图的具体关系，'
        '无意图时写口碑/地理/节奏理由)",'
        '"start_time":"HH:mm","end_time":"HH:mm","duration_min":数字,'
        '"cost":数字,"cost_currency":"cost 数字的币种代码(CNY 或目的地本币如 JPY)",'
        '"tag":"标签","remark":"实用提示(开放时间/预约方式等；禁止写任何价格与金额数字)","refs":[从参考资料编号中选，如3]}],'
        '"practical_notes":["当日可执行提示(每日必填 2-4 条：预约方式/着装要求/礼仪禁忌/交通衔接)"],'
        '"photo_spots":[{"name":"出片点位名","tip":"拍摄建议","best_time":"最佳时段"}],'
        '"backup_plan":[{"if":"触发条件","action":"可直接执行的替换方案"}],'
        '"day_options":[{"label":"方案名","summary":"一句话概述","tradeoff":"取舍说明"}]}]}。'
        "地点命名：选用参考资料中的餐厅时，poi_name 必须逐字照抄该行资料的店名（不翻译、不加分店名或地名前后缀），"
        "并输出 refs——饮食依据只挂在资料原名上；其余地点用真实存在、可被搜索到的正式或常用中文名"
        "（无可靠中文名保留正式原名，禁止编造翻译），引用资料时同样输出 refs。"
        "同一地点在 items 与叙事中保持同一写法；叙事一律用简体中文。"
        "所有地点必须真实位于目的地城市（或其合理一日游范围内）；"
        "不要输出 suggestions 备选数组，也不要在 item 中输出经纬度、poi_id 或任何来源/核验字段（由系统填写）。"
        f"共 {days} 天。{hotel_clause}。"
        "先为整趟规划各天主打片区：每天 items 集中在同一片区、按地理顺路排列并预留交通时间，"
        "相邻天安排相邻或互补片区；全程同一地点只出现一次（同一景区的多个区域/玩法合并为一条）。"
        "景点数量服从用户需求与下文结构化上限，宁少勿挤，禁止为凑数堆砌远距离点位。"
        f"每天实质活动时长（景点与餐饮停留之和，按 start_time 与 end_time 计）必须落在 "
        f"{min_active_minutes}-{max_daily_minutes} 分钟，低于或高于都会被退回重生成；慢节奏取下沿。"
        "每天安排午餐（11:00-13:30）与晚餐（17:30-20:30）各一；餐饮写目的地城市真实存在的具体餐厅，"
        "禁止「某商场美食层」等笼统称呼，禁止套用其它城市的知名餐厅。"
        "免费景点 cost 写 0，餐饮/酒店/付费景点必须写合理估算并在 cost_currency 写明币种，禁止写 0。"
        "trip_theme 只在顶层输出一次。theme 是叙事句，只提当天 items 里的地点并按游玩顺序概括主线，"
        "禁止写成「A→B→C」纯路径串。practical_notes 每日 2-4 条，每条可执行（穿什么/怎么走/需核实什么），禁止空话；"
        "没有本次参考资料支持时，不得断言必须预约、免预约、开放时刻、闭馆日或免费寄存；"
        "这些信息写成出发前核实项，不得把模型记忆当作当前官方规定。"
        "photo_spots 每日 0-4 个，backup_plan 每日 0-3 条，day_options 每日 0-2 组（当天有明显取舍分叉时输出）；"
        "用户未提供旅行意图时，photo_spots 与 day_options 可整体省略，禁止为凑数编造。"
    )
