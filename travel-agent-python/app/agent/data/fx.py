"""目的地币种与人民币粗汇率（2026-10-12：海外行程 cost 混入本币的修复）。

问题实证：东京 7 天行程（无预算）里酒店写 12000、居酒屋写 4000——那是日元，
却按 ￥ 人民币展示与合计，总价被放大约 20 倍。根因两条：
1. 「cost 一律人民币」的提示只挂在 budget_clause 里，用户没给预算时整句缺席；
2. 生成后没有任何确定性兜底（city_consumption 餐价钳制在库表无数据时不生效）。

本模块提供两件事：
- currency_clause：按目的地生成「币种 + 汇率 + 换算示例」提示句，每次生成都注入；
- backstop_item_cost：大面额币种（日元/韩元/泰铢等，1 单位远小于 1 元）的确定性兜底——
  某条 cost 按人民币看明显离谱、按本币换算后落在合理区间，才视为本币并换算，
  在 remark 里如实写明。小面额币种（美元/欧元等）本币数字按人民币看并不离谱，
  无从判别，只靠提示句约束。

汇率是粗估常量（行程费用本就标「估算」），不追实时；量级正确是唯一目标。
依赖：无（纯数据 + 纯函数）。
"""

from __future__ import annotations

#: 币种 → (中文名, 1 单位约合人民币)。粗估，量级正确即可。
_RATES: dict[str, tuple[str, float]] = {
    "JPY": ("日元", 0.048),
    "KRW": ("韩元", 0.0052),
    "THB": ("泰铢", 0.21),
    "VND": ("越南盾", 0.00028),
    "IDR": ("印尼盾", 0.00044),
    "PHP": ("菲律宾比索", 0.125),
    "TWD": ("新台币", 0.22),
    "INR": ("印度卢比", 0.085),
    "NPR": ("尼泊尔卢比", 0.053),
    "LKR": ("斯里兰卡卢比", 0.024),
    "HUF": ("福林", 0.02),
    "ISK": ("冰岛克朗", 0.052),
    "RUB": ("卢布", 0.08),
    "TRY": ("土耳其里拉", 0.18),
    "EGP": ("埃及镑", 0.15),
    "KES": ("肯尼亚先令", 0.055),
    "CLP": ("智利比索", 0.0077),
    "COP": ("哥伦比亚比索", 0.0018),
    "ARS": ("阿根廷比索", 0.007),
    "HKD": ("港币", 0.92),
    "MOP": ("澳门元", 0.89),
    "SGD": ("新加坡元", 5.4),
    "MYR": ("林吉特", 1.6),
    "USD": ("美元", 7.2),
    "CAD": ("加元", 5.2),
    "MXN": ("墨西哥比索", 0.37),
    "BRL": ("雷亚尔", 1.3),
    "PEN": ("索尔", 1.9),
    "CUP": ("古巴比索", 0.3),
    "EUR": ("欧元", 7.8),
    "GBP": ("英镑", 9.2),
    "CHF": ("瑞士法郎", 8.2),
    "CZK": ("捷克克朗", 0.31),
    "PLN": ("兹罗提", 1.8),
    "DKK": ("丹麦克朗", 1.05),
    "SEK": ("瑞典克朗", 0.68),
    "NOK": ("挪威克朗", 0.67),
    "AUD": ("澳元", 4.7),
    "NZD": ("新西兰元", 4.3),
    "AED": ("迪拉姆", 1.96),
    "QAR": ("卡塔尔里亚尔", 1.98),
    "ILS": ("新谢克尔", 1.95),
    "ZAR": ("兰特", 0.39),
    "MAD": ("摩洛哥迪拉姆", 0.72),
    "MVR": ("拉菲亚", 0.47),
}

#: 城市关键字 → 币种。按「关键字包含于城市名」匹配（「东京都」「日本东京」都命中）；
#: 未命中视为人民币（国内）。覆盖 city_iata 的海外城市 + 常见非门户目的地。
_CITY_CURRENCY: dict[str, str] = {
    **dict.fromkeys(
        [
            "东京",
            "大阪",
            "京都",
            "奈良",
            "札幌",
            "北海道",
            "冲绳",
            "名古屋",
            "福冈",
            "神户",
            "镰仓",
            "箱根",
            "横滨",
            "富士",
        ],
        "JPY",
    ),
    **dict.fromkeys(["首尔", "釜山", "济州"], "KRW"),
    **dict.fromkeys(["曼谷", "清迈", "普吉", "芭提雅", "苏梅"], "THB"),
    **dict.fromkeys(["河内", "胡志明", "岘港", "芽庄"], "VND"),
    **dict.fromkeys(["雅加达", "巴厘岛"], "IDR"),
    **dict.fromkeys(["马尼拉", "宿务", "长滩岛"], "PHP"),
    **dict.fromkeys(["台北", "高雄", "台中", "台南", "花莲"], "TWD"),
    **dict.fromkeys(["德里", "孟买", "斋浦尔"], "INR"),
    "加德满都": "NPR",
    "科伦坡": "LKR",
    "布达佩斯": "HUF",
    "雷克雅未克": "ISK",
    **dict.fromkeys(["莫斯科", "圣彼得堡"], "RUB"),
    "伊斯坦布尔": "TRY",
    "开罗": "EGP",
    "内罗毕": "KES",
    "圣地亚哥": "CLP",
    "波哥大": "COP",
    "布宜诺斯艾利斯": "ARS",
    "香港": "HKD",
    "澳门": "MOP",
    "新加坡": "SGD",
    "吉隆坡": "MYR",
    **dict.fromkeys(
        ["纽约", "洛杉矶", "旧金山", "西雅图", "芝加哥", "波士顿", "华盛顿", "拉斯维加斯", "迈阿密", "夏威夷"],
        "USD",
    ),
    **dict.fromkeys(["温哥华", "多伦多", "蒙特利尔"], "CAD"),
    **dict.fromkeys(["墨西哥城", "坎昆"], "MXN"),
    **dict.fromkeys(["圣保罗", "里约热内卢"], "BRL"),
    "利马": "PEN",
    "哈瓦那": "CUP",
    **dict.fromkeys(
        [
            "巴黎",
            "罗马",
            "米兰",
            "威尼斯",
            "佛罗伦萨",
            "巴塞罗那",
            "马德里",
            "里斯本",
            "阿姆斯特丹",
            "柏林",
            "慕尼黑",
            "法兰克福",
            "维也纳",
            "赫尔辛基",
            "都柏林",
            "布鲁塞尔",
            "雅典",
            "尼斯",
        ],
        "EUR",
    ),
    **dict.fromkeys(["伦敦", "爱丁堡"], "GBP"),
    **dict.fromkeys(["苏黎世", "日内瓦", "因特拉肯", "卢塞恩"], "CHF"),
    "布拉格": "CZK",
    "华沙": "PLN",
    "哥本哈根": "DKK",
    "斯德哥尔摩": "SEK",
    "奥斯陆": "NOK",
    **dict.fromkeys(["悉尼", "墨尔本", "布里斯班"], "AUD"),
    **dict.fromkeys(["奥克兰", "皇后镇"], "NZD"),
    **dict.fromkeys(["迪拜", "阿布扎比"], "AED"),
    "多哈": "QAR",
    "特拉维夫": "ILS",
    "约翰内斯堡": "ZAR",
    "马拉喀什": "MAD",
    "马累": "MVR",
}

#: 兜底只对「1 单位 ≤ 该值人民币」的大面额币种生效：本币数字比人民币大一个量级以上，
#: 写错时按人民币看必然离谱，才判得出来
_BACKSTOP_MAX_RATE = 0.25

#: 按人民币看「明显离谱」的单价上限（餐饮/门票为每人，酒店为每间每晚）与换算后合理下限。
#: 上限取宽：宁可漏判（交给提示句）也不把真实的人民币高价误换算
_PLAUSIBLE_CNY: dict[str, tuple[float, float]] = {
    "food": (8.0, 1000.0),
    "attraction": (5.0, 600.0),
    "hotel": (80.0, 6000.0),
}


def destination_currency(city: str | None) -> str | None:
    """目的地本币代码；国内或未知城市返回 None（即人民币）。"""
    text = str(city or "").strip()
    if not text:
        return None
    for keyword, code in _CITY_CURRENCY.items():
        if keyword in text:
            return code
    return None


def _two_sig(value: float) -> float:
    """取两位有效数字（3125 → 3100）；入参恒为正（正汇率反推的本币金额）。"""
    return float(f"{value:.2g}")


def currency_clause(city: str | None) -> str:
    """每次生成都注入的 cost 币种约束句（与预算是否给出无关）。"""
    base = "cost 一律填人民币 CNY/RMB 数值：餐饮/门票为每人每次，酒店为每间每晚。"
    code = destination_currency(city)
    if code is None or code not in _RATES:
        return base
    name, rate = _RATES[code]
    # 示例金额从「人民币 150 的一餐 / 700 的一晚」反推本币并取两位有效数字，任何面额都像样
    meal_local = _two_sig(150 / rate)
    hotel_local = _two_sig(700 / rate)
    return (
        f"{base}目的地使用{name}（{code}），1 {code} ≈ ¥{rate:g}；"
        f"你记得的本地价格必须先换算再写入 cost，例如人均 {meal_local:g} {name}的一餐写 {meal_local * rate:.0f}，"
        f"一晚 {hotel_local:g} {name}的酒店写 {hotel_local * rate:.0f}；禁止把{name}数字直接写进 cost。"
    )


def backstop_item_cost(item: dict, city: str | None) -> str | None:
    """大面额币种目的地：cost 按人民币离谱、按本币换算后合理 → 原地换算并返回备注。

    只改 item["cost"]（不追加 remark，由调用方决定落位）；不满足判据返回 None。
    """
    code = destination_currency(city)
    if code is None or code not in _RATES:
        return None
    name, rate = _RATES[code]
    if rate > _BACKSTOP_MAX_RATE:
        return None
    bounds = _PLAUSIBLE_CNY.get(str(item.get("item_type") or ""))
    if bounds is None:
        return None
    try:
        cost = float(item.get("cost"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    low, high = bounds
    converted = cost * rate
    if cost <= high or not (low <= converted <= high):
        return None
    item["cost"] = float(round(converted))
    return f"原估价 {cost:g} 为{name}，已按 1 {code}≈¥{rate:g} 换算为约 ¥{item['cost']:g}"


def backstop_item_costs(items: list[dict], city: str | None) -> None:
    """对一组 LLM 条目逐条兜底，换算说明追加进 remark。"""
    for item in items:
        note = backstop_item_cost(item, city)
        if note:
            remark = item.get("remark")
            item["remark"] = f"{remark}；{note}" if remark else note
