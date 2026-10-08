"""酒店意图识别与档次解析（G-2.6 自 hotel 拆出，包内聚模块）。

职责：
- 识别「换酒店/升级/降价/指定品牌」等住宿类意图（is_hotel_request /
  understand_hotel_intent）；
- 把口语映射到档次与入住晚次（hotel_tier / with_stay_scope /
  available_hotel_day_nos / mentioned_hotel_names）；
- 提供意图判定的原语（tiers_in_text / current_hotel_tier /
  comparison_base_tier / has_explicit_*）。

为什么单独成模块：酒店子系统原 784 行（chat_draft 最大文件），其中近三百行是
**纯口语→结构化意图的映射**，与"生成候选卡片""组织回复"两条流程无关。拆开后
hotel.py 只留候选与响应，本模块可独立单测（给定文本 → 期望意图）。

命名口径：包内相对导入，符号保留下划线（与 chat_draft 其余模块一致——
G-1.2 的"禁私有 import"约束针对跨层（api/services → agent），包内聚不受限）。

依赖：document（读取行程投影得到当前酒店/晚次）；schemas.trip。
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

from app.agent.runtime.memory import recent_turns
from app.common.llm_client import get_llm_client
from app.schemas.trip import (
    ChatTurnRequest,
)

from .intent import _cn_number

logger = logging.getLogger(__name__)

_TIER_RANK = {"经济型": 1, "舒适型": 2, "高档型": 3, "豪华型": 4, "奢华型": 5}

_TIER_KEYWORDS = {
    "经济型": ("经济", "连锁"),
    "舒适型": ("舒适", "中端", "亚朵", "全季"),
    "高档型": ("高端", "高档", "精品", "五星"),
    "豪华型": ("豪华", "五星", "国宾", "地标"),
    "奢华型": ("奢华", "国宾", "传奇", "地标", "私享"),
}

_TIER_ALIASES = {
    "经济": "经济型",
    "舒适": "舒适型",
    "中端": "舒适型",
    "高端": "高档型",
    "高档": "高档型",
    "豪华": "豪华型",
    "五星": "豪华型",
    "奢华": "奢华型",
    "顶级": "奢华型",
}

_INTENT_LABELS = {
    "cheaper": "下调一档",
    "same": "更换同档酒店",
    "better": "升级一档",
    "best": "查看最高档酒店",
    "specific": "查看指定档次",
}

_HOTEL_BRAND_ALIASES = (
    "安缦",
    "四季",
    "柏悦",
    "君悦",
    "亚朵",
    "汉庭",
    "全季",
    "桔子",
    "欢朋",
    "温德姆",
    "康莱德",
    "香格里拉",
    "索菲特",
    "紫萱",
    "西子宾馆",
    "黄龙饭店",
)


@dataclass
class HotelIntent:
    action: str
    target_tier: str
    base_tier: str
    requested_nights: int = 0
    requested_day_nos: tuple[int, ...] = ()
    requested_hotel_names: tuple[str, ...] = ()
    invalid_scope: bool = False


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _tiers_in_text(value: str | None) -> list[str]:
    text = value or ""
    found = {tier for tier in _TIER_RANK if tier in text}
    for alias, tier in _TIER_ALIASES.items():
        if alias in text:
            found.add(tier)
    return sorted(found, key=lambda tier: _TIER_RANK[tier])


def _preference_tier(value: str | None) -> str:
    tiers = _tiers_in_text(value)
    return tiers[-1] if tiers else "舒适型"


def _hotel_tier(hotel: dict) -> str:
    text = (hotel.get("name") or "") + (hotel.get("description") or "") + (hotel.get("tags") or "")
    explicit = [tier for tier in _TIER_RANK if tier in text]
    if explicit:
        return max(explicit, key=lambda tier: _TIER_RANK[tier])
    matched = [tier for tier, keywords in _TIER_KEYWORDS.items() if any(keyword in text for keyword in keywords)]
    return max(matched, key=lambda tier: _TIER_RANK[tier], default="舒适型")


def _mentioned_hotel_names(message: str, hotels: list[dict]) -> tuple[str, ...]:
    normalized = message.replace("安曼", "安缦")
    matched = []
    for hotel in hotels:
        name = str(hotel.get("name") or "")
        city = str(hotel.get("city") or "")
        short_name = name.removeprefix(city) if city else name
        alias_hit = any(alias in normalized and alias in name for alias in _HOTEL_BRAND_ALIASES)
        if name and (name in normalized or (len(short_name) >= 4 and short_name in normalized) or alias_hit):
            matched.append(name)
    return tuple(matched)


#: 「酒店不动」短路（M4 §9.1）：用户明确保留住宿时，名词"酒店"不得劫持进候选流
_KEEP_HOTEL_RE = re.compile(r"酒店[^。？！.]{0,8}(不动|不换|不变|不调整|不修改|不升级|保留)|保留[^。？！.]{0,8}酒店")


def _is_hotel_request(req: ChatTurnRequest, hotels: list[dict] | None = None) -> bool:
    normalized_message = req.message.replace("安曼", "安缦")
    # M4（spec §9.1）：「酒店不动」优先于历史换酒店话题与名词匹配——明确保留
    # 住宿且本轮没有更换动作词时，不是酒店请求（时间修改走 plan_update）。
    if _KEEP_HOTEL_RE.search(normalized_message) and not re.search(
        r"(?<!不)换|升级|降一档|降级|便宜|推荐", normalized_message
    ):
        return False
    # 当前轮明确谈景点/餐饮/行程时，不能被前几轮酒店上下文中的“其他、便宜”等词劫持。
    if re.search(r"景点|景区|餐厅|餐饮|美食|行程|安排|路线|路线", normalized_message) and not re.search(
        r"酒店|住宿|宾馆|客栈|房型|入住|住一晚|住几晚", normalized_message
    ):
        return False
    if _mentioned_hotel_names(normalized_message, hotels or []):
        return True
    if re.search(
        r"酒店|住宿|宾馆|客栈|民宿|旅馆|住处|住的地方|下榻|过夜|换个住|换住宿|"
        r"升级|降一档|档次低|低一点|低些|便宜点|便宜一点|便宜些|省钱|预算友好|"
        r"更好|好一点|好些|贵一点|贵些|高级些|档次高|"
        r"高端|高档|豪华|奢华|五星|顶级|顶配|最屌|最高级|最牛",
        req.message,
    ):
        return True
    recent = " ".join(str(item.get("content") or "") for item in (req.history or [])[-2:])
    return bool(
        re.search(r"酒店|住宿|宾馆", recent)
        and re.search(r"其他|其它|别的|还有|换一个|换一家|便宜|贵点|好点|更好|同档|同级", req.message)
    )


def _current_hotel_names(req: ChatTurnRequest) -> set[str]:
    return {
        str(item.get("poi_name"))
        for plan in req.plans
        for item in (plan.get("items") or [])
        if item.get("item_type") == "hotel" and item.get("poi_name")
    }


def _available_hotel_day_nos(req: ChatTurnRequest) -> list[int]:
    """已排酒店的入住晚次（去重升序）。

    用显式循环而非集合推导：类型守卫（isinstance）与取值需在同一作用域，
    否则类型检查无法收窄 `plan.get("day_no")`（原先靠 int() 强转掩盖了这点）。
    """
    days: set[int] = set()
    for plan in req.plans:
        day_no = plan.get("day_no")
        if not isinstance(day_no, int):
            continue
        if any(item.get("item_type") == "hotel" for item in (plan.get("items") or [])):
            days.add(day_no)
    return sorted(days)


def _has_explicit_stay_scope(message: str) -> bool:
    return bool(
        re.search(
            r"最后(?:一)?(?:天|晚)|末(?:天|晚)|第\s*[0-9一二两三四五六七八九十]+\s*(?:天|晚)|"
            r"[0-9一二两三四五六七八九十]+\s*晚",
            message or "",
        )
    )


def _with_stay_scope(intent: HotelIntent, req: ChatTurnRequest, hotels: list[dict]) -> HotelIntent:
    available = _available_hotel_day_nos(req)
    valid_day_nos = set(range(1, req.days + 1))
    scope_mentioned = _has_explicit_stay_scope(req.message)
    # 入住范围只认用户本轮原话：未提晚次时默认当前全部住宿晚次；明确说
    # “住一晚”但没说哪晚时留空供前端选择；“第 N 晚/最后一晚”则精确选中。
    day_nos = [] if scope_mentioned else list(available)
    invalid_scope = False
    if re.search(r"最后(?:一)?天|末天", req.message):
        day_nos.append(req.days)
    elif re.search(r"最后(?:一)?晚|末晚", req.message):
        day_nos.append(available[-1] if available else req.days)
    day_values = re.findall(r"第\s*([0-9一二两三四五六七八九十]+)\s*(?:天|晚)", req.message)
    for value in day_values:
        day_no = _cn_number(value)
        if day_no in valid_day_nos and day_no not in day_nos:
            day_nos.append(day_no)
        elif day_no is not None and day_no not in valid_day_nos:
            invalid_scope = True
    night_match = re.search(r"([0-9一二两三四五六七八九十]+)\s*晚", req.message)
    if night_match and re.search(r"第\s*$", req.message[: night_match.start()]):
        night_match = None
    requested_nights = (
        _cn_number(night_match.group(1))
        if night_match
        else (len(available) if not scope_mentioned and available else intent.requested_nights)
    )
    if requested_nights and requested_nights > len(valid_day_nos):
        invalid_scope = True
    if day_nos:
        requested_nights = len(day_nos)
    max_scope_nights = req.days if scope_mentioned else (len(available) or req.days)
    requested_nights = max(1, min(requested_nights or len(available) or req.days, max_scope_nights))

    named = _mentioned_hotel_names(req.message, hotels)
    if named:
        named_tiers = [_hotel_tier(hotel) for hotel in hotels if hotel.get("name") in named]
        target = max(named_tiers, key=lambda tier: _TIER_RANK[tier])
        intent = HotelIntent(action="specific", target_tier=target, base_tier=intent.base_tier)
    return HotelIntent(
        action=intent.action,
        target_tier=intent.target_tier,
        base_tier=intent.base_tier,
        requested_nights=requested_nights,
        requested_day_nos=tuple(day_nos),
        requested_hotel_names=named,
        invalid_scope=invalid_scope,
    )


def _current_hotel_tier(req: ChatTurnRequest, hotels: list[dict]) -> str:
    current_names = _current_hotel_names(req)
    known = [_hotel_tier(hotel) for hotel in hotels if hotel.get("name") in current_names]
    if known:
        return max(known, key=lambda tier: _TIER_RANK[tier])
    return _preference_tier(req.hotel_tier)


def _previous_proposed_hotel_tier(req: ChatTurnRequest) -> str | None:
    for turn in reversed(req.history or []):
        if turn.get("role") not in {"ai", "assistant"}:
            continue
        content = str(turn.get("content") or "").replace("*", "")
        match = re.search(
            r"本次提供\s*\d+\s*家\s*(经济型|舒适型|高档型|豪华型|奢华型)酒店",
            content,
        )
        if match:
            return match.group(1)
    return None


def _hotel_comparison_base_tier(req: ChatTurnRequest, hotels: list[dict]) -> str:
    if re.search(r"再|继续|还要更|还想更", req.message):
        return _previous_proposed_hotel_tier(req) or _current_hotel_tier(req, hotels)
    return _current_hotel_tier(req, hotels)


def _fallback_hotel_intent(message: str, base_tier: str) -> HotelIntent:
    if re.search(r"最屌|最好|最贵|最高级|最高档|顶级|顶配|天花板|最牛", message):
        action = "best"
    elif re.search(r"便宜|省钱|实惠|预算友好|降一档|低一档|档次低|低一点|低些|降低|少花", message):
        action = "cheaper"
    elif re.search(r"其他|其它|别的|换一家|换一个|换(?:个|一家)?酒店|换住宿|换住处|同档|同级|类似", message):
        action = "same"
    elif re.search(r"更好|好一点|好点|好些|升级|升一档|高一档|贵一点|贵些|高级一点|高级些|档次高", message):
        action = "better"
    else:
        explicit = _tiers_in_text(message)
        action = "specific" if explicit else "same"

    base_rank = _TIER_RANK[base_tier]
    explicit = _tiers_in_text(message)
    if action == "best":
        target = "奢华型"
    elif action == "cheaper":
        target = next(tier for tier, rank in _TIER_RANK.items() if rank == max(base_rank - 1, 1))
    elif action == "better":
        target = next(tier for tier, rank in _TIER_RANK.items() if rank == min(base_rank + 1, 5))
    elif action == "specific" and explicit:
        target = explicit[-1]
    else:
        target = base_tier
    return HotelIntent(action=action, target_tier=target, base_tier=base_tier)


def _has_explicit_hotel_comparison(message: str) -> bool:
    return bool(
        re.search(
            r"便宜|省钱|实惠|预算友好|降一档|低一档|档次低|低一点|低些|降低|少花|"
            r"其他|其它|别的|换一家|换一个|换(?:个|一家)?酒店|换住宿|换住处|"
            r"同档|同级|类似|更好|好一点|好点|好些|升级|升一档|高一档|贵一点|贵些|高级一点|高级些|档次高|"
            r"最屌|最好|最贵|最高级|最高档|顶级|顶配|天花板|最牛|安缦|安曼|四季|希尔顿|"
            r"香格里拉|亚朵|汉庭|如家",
            message,
        )
    )


def _normalized_hotel_intent(action: str, target: str, base_tier: str) -> HotelIntent:
    """不信任模型计算档次，只采纳语义动作；档次移动由代码确定，避免跨错档。"""
    base_rank = _TIER_RANK[base_tier]
    if action == "cheaper":
        target_rank = max(base_rank - 1, 1)
    elif action == "same":
        target_rank = base_rank
    elif action == "better":
        target_rank = min(base_rank + 1, 5)
    elif action == "best":
        target_rank = 5
    else:
        target_rank = _TIER_RANK.get(target, base_rank)
    normalized = next(tier for tier, rank in _TIER_RANK.items() if rank == target_rank)
    return HotelIntent(action=action, target_tier=normalized, base_tier=base_tier)


def _understand_hotel_intent(req: ChatTurnRequest, hotels: list[dict]) -> HotelIntent:
    """先理解相对当前酒店的换房意图，再把它收敛为一个明确目标档次。"""
    base_tier = _hotel_comparison_base_tier(req, hotels)
    fallback = _fallback_hotel_intent(req.message, base_tier)
    history = recent_turns(req.history, turns=4, max_chars=500)
    try:
        raw = (
            get_llm_client()
            .complete(
                "请识别用户更换酒店的真实意图，并只输出JSON。\n"
                "action只能是 cheaper、same、better、best、specific 之一："
                "便宜一点是cheaper；看看其他/换一家是same；更好一点/升级是better；"
                "最好/最顶级/最屌是best；明确说某档次是specific。\n"
                "档次从低到高：经济型、舒适型、高档型、豪华型、奢华型。"
                "cheaper和better都只移动一档，same保持当前档，best为奢华型。"
                "如果用户用“再”承接上文，可结合最近对话判断最终目标。\n"
                f"当前酒店档次：{base_tier}；初始住宿偏好：{req.hotel_tier or '未指定'}；"
                f"最近对话：{json.dumps(history, ensure_ascii=False)}；用户原话：{req.message}\n"
                '格式：{"action":"better","target_tier":"豪华型"}',
                system_prompt="你是酒店换房意图分类器。理解口语和相对比较，不推荐酒店，不输出解释。",
                temperature=0.0,
            )
            .strip()
        )
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        action = str(data.get("action") or "")
        target = str(data.get("target_tier") or "")
        if action in _INTENT_LABELS and target in _TIER_RANK:
            expected = _fallback_hotel_intent(req.message, base_tier)
            # 明确比较词优先服从确定性规则，避免模型把“看看其他酒店”错误继承成
            # 上一轮的“便宜一点”，或把“最好的”弱化为普通升级。
            if _has_explicit_hotel_comparison(req.message):
                return _with_stay_scope(expected, req, hotels)
            return _with_stay_scope(_normalized_hotel_intent(action, target, base_tier), req, hotels)
    except Exception as exc:
        logger.warning("hotel intent classification failed: %s", exc)
    return _with_stay_scope(fallback, req, hotels)
