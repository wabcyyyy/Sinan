"""存在性判定：一个名字指的地点是否真的存在（PLAN-A1 G2 + 09-19 G0 复评）。

三值结论，**且只有两种证据够格删点位**：

- `VERIFIED` —— 解析器给出同一实体（别名集合过名称门槛）且落在目的地范围内；
- `NOT_FOUND` —— provider 成功应答并明确回了"没有"；
- `UNKNOWN` —— 没问到（未启用/超时/被限流/结果歧义被门槛拒收/预算耗尽）。

量测依据（`docs/量测-存在性接地-2026-09-18.md`，192 个模型自选点位名）：
Nominatim 的"空结果"里约 **70% 是真实地点**（含秦始皇兵马俑、东京迪士尼），
OpenTripMap 的半径池按热度截断 top-40、对"不存在"根本无资格下结论。所以
**NOT_FOUND 一律不删**，除非给出它的 provider 自带 `authoritative_negative=True`
（接入高德/Google Places 后自动生效，无需改判定代码）。唯一随时有效的删除信号
是 `out_of_area`：名字解析成功了，但解析到的实体在别的城市/国家——这是与本次
行程矛盾的正面证据，不是"我没查到"。

约束（后来人别当性能坑）：
- 缺 key / 超时 / 被限流 → **UNKNOWN，绝不允许当成"不存在"**；
- 无 key 的 provider 连请求都不发（`available()` 先判）；
- 一切外呼走 `app.common.external_client.fetch_json`（超时 + 字节上限，INV-9）；
- Nominatim 公共实例 1 rps，车道间隔由 `places._nominatim_client` 保证；
- 单次 run 的解析次数受 `existence_resolve_limit` 约束，耗尽记 UNKNOWN 并计数。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.agent.core.poi_identity import haversine_m, strip_name_annotation
from app.agent.data import places
from app.agent.grounding.grounding_evidence import issue_evidence
from app.agent.runtime.run_limits import RunLimitExceeded, current_limits
from app.common.config import settings

logger = logging.getLogger(__name__)

VERIFIED = "verified"
NOT_FOUND = "not_found"
UNKNOWN = "unknown"

# 名称匹配阈值：Jaccard 字符重叠 + 最少共享字符数。0.55/4 的取值理由见
# `same_entity`（量测里把 overlap 放宽到 3 会放进「明婷小馆→报名大厅」这类误配）。
_MIN_SHARED_CHARS = 4


@dataclass(frozen=True)
class ResolveResult:
    """一次存在性判定的全部产出；`provider` 只在 VERIFIED 时有意义。"""

    state: str
    provider: str = ""
    name: str | None = None
    external_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    reason: str = ""
    out_of_area: bool = False
    authoritative_negative: bool = False

    @property
    def grounded(self) -> bool:
        """可作为背书与坐标来源的结论：真存在、且在目的地范围内。"""
        return self.state == VERIFIED and not self.out_of_area

    @property
    def deletable(self) -> bool:
        """够格把点位从行程里删掉的证据（09-19 复评的两条）。"""
        if self.out_of_area:
            return True
        return self.state == NOT_FOUND and self.authoritative_negative

    @classmethod
    def unknown(cls, reason: str) -> ResolveResult:
        return cls(state=UNKNOWN, reason=reason)


class ExistenceProvider(Protocol):
    """可插拔的存在性数据源。"""

    name: str
    # 该源的"空结果"是否等价于"不存在"。免费源一律 False（见模块 docstring）。
    authoritative_negative: bool

    def available(self) -> bool: ...

    def resolve(self, name: str, city: str) -> ResolveResult: ...


def same_entity(query: str, candidate: str) -> bool:
    """同一实体判定：外部检索对任意输入都会尽力返回，不设门槛就会把
    「不存在的景点」锚定到无关坐标（典型事故：西安「方所书店」→ 安徽某防治所）。

    规则：归一（去空白/标点/大小写）后相等或互为包含 → 同一实体；否则要求
    字符重叠率 ≥ `entity_name_similarity_min` **且** 共享字符 ≥ 4——只有比率
    不够，短名（"老街"vs"老城"）比率高却不是同一处。
    """

    def norm(value: str) -> str:
        return "".join(ch for ch in strip_name_annotation(value).lower() if ch.isalnum())

    q, c = norm(query), norm(candidate)
    if not q or not c:
        return False
    if q == c or q in c or c in q:
        return True
    overlap = len(set(q) & set(c))
    union = len(set(q) | set(c))
    return union > 0 and overlap / union >= settings.entity_name_similarity_min and overlap >= _MIN_SHARED_CHARS


def matches_any(query: str, spellings: list[str] | tuple[str, ...] | None) -> bool:
    """对 provider 返回的全部名称写法（本地名 + `name:xx` 别名）逐一判等。

    跨脚本匹配只能靠别名：`淺草寺` 的 `name:ja` 是 `浅草寺`、`奧賽博物館` 的
    `name:zh-Hans` 是 `奥赛博物馆`——简繁/中英写法差异占实测未判定的 56%。
    """
    return any(same_entity(query, text) for text in spellings or [])


# ---- 目的地范围闸 -----------------------------------------------------------

_center_memo: dict[str, tuple[float, float] | None] = {}
#: 表本身也必须有界（按城市数天然不大，但它是进程级、只增不减的）
_CENTER_MEMO_MAX = 256


def city_center(city: str, memo: dict[str, Any] | None = None) -> tuple[float, float] | None:
    """城市名 → 中心坐标（OTM geoname → Nominatim）；结果按城市记忆化。"""
    store = _center_memo if memo is None else memo
    name = str(city or "").strip()
    if not name:
        return None
    if name in store:
        return store[name]
    center: tuple[float, float] | None = None
    geo = places.resolve_city_center(name)
    if geo:
        center = (float(geo["latitude"]), float(geo["longitude"]))
    else:
        hit = places.geocode_place(name)
        if hit:
            center = (float(hit["latitude"]), float(hit["longitude"]))
    if center is None:
        # 只记忆化成功解析。None 既可能是"这城市确实没有中心点"，也可能是 geocoder
        # 超时/并发下抢不到槽位（`places` 对这类负答案刻意配了 negative_ttl=300s，
        # 就是不许把它当永久事实），而进程级永久缓存会让目的地范围闸对该城市
        # **永久失效**——out_of_area 是本模块唯一任何时候都成立的否证信号。
        return None
    if len(store) >= _CENTER_MEMO_MAX:
        store.clear()
    store[name] = center
    return center


def _in_destination_area(latitude: float, longitude: float, city: str) -> bool | None:
    """True=在目的地范围内；False=明确在别处；None=没有中心坐标、无法判。"""
    center = city_center(city)
    if center is None:
        return None
    return haversine_m(center[0], center[1], latitude, longitude) <= settings.existence_area_radius_m


# ---- provider 实现 ----------------------------------------------------------


class OpenTripMapProvider:
    """OpenTripMap：按城市取热门点池再匹配名称。**只能正向证实**——池是按热度
    截断的 top-N，不在池里不等于不存在，所以本类永不返回 NOT_FOUND。
    """

    name = "opentripmap"
    authoritative_negative = False

    def available(self) -> bool:
        return places.otm_enabled()

    def resolve(self, name: str, city: str) -> ResolveResult:
        center = city_center(city)
        if center is None:
            return ResolveResult.unknown("city_center_unresolved")
        kinds_by_category = (("attraction", "interesting_places"), ("food", "foods"))
        for _category, kinds in kinds_by_category:
            for row in places.search_places_near(
                center[0], center[1], city=city, kinds=kinds, limit=settings.otm_limit
            ):
                if same_entity(name, str(row.get("name") or "")):
                    lat, lng = row.get("latitude"), row.get("longitude")
                    if lat is None or lng is None:
                        continue
                    return ResolveResult(
                        state=VERIFIED,
                        provider=self.name,
                        name=row.get("name"),
                        external_id=str(row.get("xid") or "") or None,
                        latitude=float(lat),
                        longitude=float(lng),
                        address=row.get("address"),
                    )
        return ResolveResult.unknown("pool_miss_not_authoritative")


class NominatimProvider:
    """Nominatim（OSM 官方地理编码）：唯一能按名字直查实体的免费源。"""

    name = "nominatim"
    # 实测：空结果里约 70% 是真实地点（OSM 对国内餐饮/酒店覆盖薄），否证无资格。
    authoritative_negative = False

    def available(self) -> bool:
        return bool(settings.nominatim_enabled)

    def resolve(self, name: str, city: str) -> ResolveResult:
        rows = places.geocode_place_rows(name, city, namedetails=True)
        if rows is None:
            return ResolveResult.unknown("provider_unavailable")
        if not rows:
            return ResolveResult(state=NOT_FOUND, provider=self.name, reason="provider_empty")
        return pick_row(rows, name, city, provider=self.name, spellings_of=_nominatim_spellings)


def _nominatim_spellings(row: dict[str, Any]) -> list[str]:
    text = str(row.get("name") or "").strip()
    aliases = [str(a) for a in row.get("aliases") or []]
    return [text, *aliases] if text else aliases


def pick_row(rows: list[dict[str, Any]], name: str, city: str, *, provider: str, spellings_of: Any) -> ResolveResult:
    """在候选里挑"同一实体 + 落在目的地"的那一条。

    先要名字对得上，再看位置：名字对得上但落在别的城市，是 `out_of_area`
    （可删的矛盾证据），不是 VERIFIED。
    """
    first_mismatch: ResolveResult | None = None
    for row in rows:
        spellings = spellings_of(row)
        if not matches_any(name, spellings):
            continue
        lat, lng = row.get("latitude"), row.get("longitude")
        if lat is None or lng is None:
            continue
        south_east = (float(lat), float(lng))
        result = ResolveResult(
            state=VERIFIED,
            provider=provider,
            name=spellings[0] if spellings else row.get("name"),
            external_id=str(row.get("place_id") or row.get("external_id") or "") or None,
            latitude=south_east[0],
            longitude=south_east[1],
            address=str(row.get("display_name") or row.get("address") or "") or None,
        )
        if _in_destination_area(south_east[0], south_east[1], city) is False:
            first_mismatch = first_mismatch or replace(
                result, state=UNKNOWN, out_of_area=True, reason="resolved_out_of_area"
            )
            continue
        return result
    if first_mismatch is not None:
        return first_mismatch
    return ResolveResult.unknown("name_gate_rejected")


# 免费源常驻；收费留白源见 `existence_commercial`（它要用本模块的结论类型，
# 顶层互相 import 会成环，所以在注册表里延迟导入）。
_FREE_PROVIDERS: dict[str, ExistenceProvider] = {
    "otm": OpenTripMapProvider(),
    "opentripmap": OpenTripMapProvider(),
    "nominatim": NominatimProvider(),
}
_PROVIDERS: dict[str, ExistenceProvider] | None = None


def provider_registry() -> dict[str, ExistenceProvider]:
    global _PROVIDERS
    if _PROVIDERS is None:
        from app.agent.grounding import existence_commercial

        _PROVIDERS = {
            **_FREE_PROVIDERS,
            "amap": existence_commercial.AmapProvider(),
            "google_places": existence_commercial.GooglePlacesProvider(),
        }
    return _PROVIDERS


def providers_in_order() -> list[ExistenceProvider]:
    """按配置顺序取 provider；未注册的配置项忽略（拼错键不该让生成崩）。"""
    registry = provider_registry()
    wanted = [part.strip() for part in str(settings.existence_provider_order or "").split(",") if part.strip()]
    out = [registry[key] for key in wanted if key in registry]
    return out or [registry["otm"], registry["nominatim"]]


def resolve_poi(name: str, city: str) -> ResolveResult:
    """存在性判定唯一入口：按配置顺序问，命中即止；全未判定才算未判定。"""
    target = str(name or "").strip()
    if not target:
        return ResolveResult.unknown("empty_name")
    key = f"{str(city or '').strip()}|{target}"
    cached = _memo.get(key)
    if cached is not None:
        return cached

    result = _run_providers(target, city)
    if result.grounded:
        # 判定成功当场签发证据票：之后这行数据在任何链路被引用，背书凭的是票
        issue_evidence(
            {
                "name": result.name or target,
                "city": city,
                "source": result.provider,
                "external_id": result.external_id or "",
                "latitude": result.latitude,
                "longitude": result.longitude,
            }
        )
    if result.state != UNKNOWN:
        # 只记忆化**判定**（VERIFIED / NOT_FOUND）。UNKNOWN 绝大多数是"没问到"：
        # 额度耗尽、源不可用、超时。_memo 是进程级、跨 run 的，缓存它等于让一次
        # 限流把该 (城市,名字) 永久钉死成解析不出来，并传染给之后每个用户。
        # 不缓存也不会失控：每次重问都照样 `record_existence()` 扣额度。
        if len(_memo) >= _MEMO_MAX:  # 有界：判定结果稳定，最旧的 1024 条之外直接丢
            _memo.clear()
        _memo[key] = result
    return result


_MEMO_MAX = 1024
_memo: dict[str, ResolveResult] = {}


def reset_existence_state() -> None:
    """测试/评测用：清掉城市中心与判定结果记忆。"""
    _memo.clear()
    _center_memo.clear()


def _run_providers(target: str, city: str) -> ResolveResult:
    unknowns: list[str] = []
    negative = False
    for provider in providers_in_order():
        if not provider.available():
            unknowns.append(f"{provider.name}:not_configured")
            continue
        if not _budget_allows():
            unknowns.append("budget_exhausted")
            break
        try:
            result = provider.resolve(target, city)
        except Exception as exc:  # provider 故障绝不影响生成：按未判定处理
            logger.warning("existence provider %s failed: %s", provider.name, exc)
            unknowns.append(f"{provider.name}:error")
            continue
        _consume_budget()
        if result.grounded:
            return result
        if result.out_of_area:
            return result
        if result.state == NOT_FOUND:
            negative = negative or provider.authoritative_negative
            unknowns.append(f"{provider.name}:not_found")
            continue
        unknowns.append(f"{provider.name}:{result.reason or 'unknown'}")
    if negative:
        return ResolveResult(state=NOT_FOUND, authoritative_negative=True, reason="; ".join(unknowns))
    return ResolveResult.unknown("; ".join(unknowns) or "no_provider")


def _budget_allows() -> bool:
    limits = current_limits()
    return limits is None or limits.max_existence_checks <= 0 or limits.existence_checks < limits.max_existence_checks


def _consume_budget() -> None:
    limits = current_limits()
    if limits is None:
        return
    try:
        limits.record_existence()
    except RunLimitExceeded:
        return
