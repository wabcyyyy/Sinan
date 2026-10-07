"""行程读路径（移植自 Java `ItineraryQueryService`）。

VO 字段名逐字保留（`schemaVersion`/`whyThis`/`factEvidence`/`dayOptions`/…）：
前端 `src/types/itinerary.ts` 按这些键取值，改名是**静默**的字段丢失。

三条容易丢的语义，都在这里显式实现并注释了原因：
1. 整条行程的点位**一次查出再分组**（避免按天 N+1）；预算合计用一次 `IN` 查询；
2. `stayNights` 以主表落库值为准，缺失才回落 `天数-1`——按"有酒店项的天数"反推会
   少算最后一晚（退房日没有酒店项）；
3. `qualityStatus` 的判定顺序（BLOCKED → DRAFT → 空 → STALE → READY/WARNINGS）
   依赖 days 与 items 的原始行，不能只看最终 status 字段。

时间序列化按 Jackson `ISO_LOCAL_TIME` / `ISO_LOCAL_DATE_TIME` 的"零分量省略"行为，
否则 Java 发 "09:30"、Python 发 "09:30:00"，前端切片显示会出现不一致。
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select

from app.agent import (
    is_authoritative_source,
    live_quote_quota_exhausted,
    search_live_flight_quotes,
    search_live_hotel_quotes,
    shape_quote_for_wire,
)
from app.common import cache_store
from app.common.envelope import ApiError
from app.common.vo_json import iso_date, iso_datetime, iso_time, number
from app.db.models import BudgetDetail, ItineraryDay, ItineraryItem, ItineraryMain, ItineraryMember
from app.db.session import session_scope

logger = logging.getLogger(__name__)

DETAIL_CACHE_NAMESPACE = "itinerary:detail"
DETAIL_CACHE_TTL_SECONDS = 600  # 与 Java RedisCacheConfig 的 itinerary:detail 10min 一致
QUALITY_RULE_VERSION = "travel-quality-1.2"
SCHEMA_VERSION = "1.0"

_INCOMPLETE_DAY_STATUSES = {"PENDING", "RUNNING"}
_FAILED_DAY_STATUSES = {"FAILED", "TIMED_OUT_UNKNOWN"}
# 列表视图白名单（SPEC v2.3 §6.5）：favorite/archived 之外的档位都排除归档行
_LIST_VIEWS = {"all", "active", "done", "favorite", "archived"}


_iso_time = iso_time
_iso_datetime = iso_datetime
_iso_date = iso_date
_num = number


def _loads_object(raw: str | None) -> dict[str, Any] | None:
    """损坏的可选 JSON 列不应阻塞详情读取（Java 同样降级为 null 并记日志）。"""
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("json column parse failed, returned null instead")
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [str(item) for item in value if item is not None]


def _as_map(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_map_list(value: Any) -> list[dict[str, Any]] | None:
    return value if isinstance(value, list) else None


def _member_role(session, itinerary_id: int, user_id: int) -> str | None:
    """协作者角色（editor/viewer）；非成员返回 None。owner 不在成员表。"""
    return session.execute(
        select(ItineraryMember.role).where(
            ItineraryMember.itinerary_id == itinerary_id, ItineraryMember.user_id == user_id
        )
    ).scalar_one_or_none()


def require_main(session, user_id: int, itinerary_id: int) -> ItineraryMain:
    """读闸门（SPEC C2.3）：owner 或任意协作成员（editor/viewer）可读。

    非成员同样报 404，不暴露资源是否存在（同 Java）。写路径不要用本函数，
    用 `require_writable_main`；owner 专属操作（删行程/分享/封面/收藏归档）
    用 `require_owned_main`。
    """
    main = session.get(ItineraryMain, itinerary_id)
    if main is None or (main.user_id != user_id and _member_role(session, itinerary_id, user_id) is None):
        raise ApiError(404, "行程不存在")
    return main


def require_writable_main(session, user_id: int, itinerary_id: int) -> ItineraryMain:
    """写闸门（SPEC C2.3）：owner 或 editor；viewer 明确 403（非成员仍 404）。"""
    main = session.get(ItineraryMain, itinerary_id)
    if main is None:
        raise ApiError(404, "行程不存在")
    if main.user_id != user_id and _member_role(session, itinerary_id, user_id) != "editor":
        if _member_role(session, itinerary_id, user_id) is None:
            raise ApiError(404, "行程不存在")
        raise ApiError(403, "对该行程只有查看权限")
    return main


def require_owned_main(session, user_id: int, itinerary_id: int) -> ItineraryMain:
    """owner 专属闸门：删行程/分享/封面/收藏归档等个人语义操作不给协作者。"""
    main = session.get(ItineraryMain, itinerary_id)
    if main is None or main.user_id != user_id:
        raise ApiError(404, "行程不存在")
    return main


def next_sort(session, day_id: int | None) -> int:
    """某日末尾位次：`max(sort_no) + 1`，空日为 0。

    只留这一份实现——Java 在 `ItineraryCommandService:399` 与 `ItineraryPlanApplyService:320`
    各写了一份同名 `nextSort`，这类原语一旦分叉就会出现「同一天里两个入口算出不同顺序」。
    """
    highest = session.execute(
        select(func.max(ItineraryItem.sort_no)).where(ItineraryItem.day_id == day_id)
    ).scalar_one_or_none()
    return int(highest if highest is not None else -1) + 1


def find_owned_main(user_id: int, itinerary_id: int) -> ItineraryMain:
    """owner 专属校验 + 取主表实体（同 Java `findOwnedMain`）。

    **不要 expunge**：写路径常在同一个事务里先取实体、再打快照（`create_snapshot` →
    `detail` → 这里），一旦 expunge，调用方手里那个实例就脱离会话，之后对它的赋值
    会被静默丢弃——不报错、不落库。MyBatis 没有这个坑（`updateById` 是显式 UPDATE）。
    离开会话后仍可安全读属性，是因为 sessionmaker 配了 `expire_on_commit=False`。
    """
    with session_scope() as session:
        return require_owned_main(session, user_id, itinerary_id)


def find_readable_main(user_id: int, itinerary_id: int) -> ItineraryMain:
    """读校验（owner/成员）+ 取实体；SSE events 等路由级读检查用。"""
    with session_scope() as session:
        return require_main(session, user_id, itinerary_id)


def find_writable_main(user_id: int, itinerary_id: int) -> ItineraryMain:
    """写校验（owner/editor）+ 取实体；SSE 写入口（chat-edit/stream）等用。"""
    with session_scope() as session:
        return require_writable_main(session, user_id, itinerary_id)


def list_summaries(user_id: int, view: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
    """行程列表（id 倒序、无分页）。view/q 语义见 SPEC v2.3 §6.5。

    `active` 必须含 `gen_state IS NULL` 分支：NULL 是迁移前存量（V1 DDL 注释），
    漏掉会让老行程从「计划中」里凭空消失；归档语义独立——`archived` 档只列归档，
    其余档位（含 favorite）一律排除归档行。
    """
    normalized_view = (view or "all").strip() or "all"
    if normalized_view not in _LIST_VIEWS:
        raise ApiError(400, f"无效的视图过滤：{normalized_view}")
    keyword = (q or "").strip()

    with session_scope() as session:
        stmt = select(ItineraryMain).where(ItineraryMain.user_id == user_id)
        if normalized_view == "archived":
            stmt = stmt.where(ItineraryMain.archived.is_(True))
        else:
            stmt = stmt.where(ItineraryMain.archived.is_(False))
            if normalized_view == "active":
                stmt = stmt.where(or_(ItineraryMain.gen_state.is_(None), ItineraryMain.gen_state != "COMPLETED"))
            elif normalized_view == "done":
                stmt = stmt.where(ItineraryMain.gen_state == "COMPLETED")
            elif normalized_view == "favorite":
                stmt = stmt.where(ItineraryMain.favorite.is_(True))
        if keyword:
            pattern = f"%{keyword}%"
            stmt = stmt.where(or_(ItineraryMain.title.like(pattern), ItineraryMain.city.like(pattern)))
        mains = session.execute(stmt.order_by(ItineraryMain.id.desc())).scalars().all()
        if not mains:
            return []
        totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        budgets = (
            session.execute(select(BudgetDetail).where(BudgetDetail.itinerary_id.in_([main.id for main in mains])))
            .scalars()
            .all()
        )
        for detail in budgets:
            totals[detail.itinerary_id] += detail.amount or Decimal("0")
        return [
            {
                "id": main.id,
                "title": main.title,
                "city": main.city,
                "startDate": _iso_date(main.start_date),
                "endDate": _iso_date(main.end_date),
                "days": main.days,
                "persons": main.persons,
                "budget": _num(main.budget),
                "totalAmount": float(totals.get(main.id, Decimal("0"))),
                "status": main.status,
                "tripTheme": main.trip_theme,
                # S1：封面/收藏/归档/分享态（hasShare 只回布尔，token 不下发列表）
                "coverUrl": main.cover_url,
                "coverSource": main.cover_source,
                "coverCredit": _loads_object(main.cover_credit),
                "favorite": bool(main.favorite),
                "archived": bool(main.archived),
                "hasShare": main.share_token is not None,
                "createdAt": _iso_datetime(main.created_at),
            }
            for main in mains
        ]


def detail(user_id: int, itinerary_id: int) -> dict[str, Any]:
    cache_key = f"{user_id}:{itinerary_id}"
    cached = cache_store.get_json(DETAIL_CACHE_NAMESPACE, cache_key)
    if cached is not None:
        return cached
    payload = build_detail(user_id, itinerary_id)
    cache_store.set_json(DETAIL_CACHE_NAMESPACE, cache_key, payload, DETAIL_CACHE_TTL_SECONDS)
    return payload


def evict_detail(user_id: int, itinerary_id: int) -> None:
    """写路径的精确失效（对应 Java 侧生成链路的 evictDetailCache；不用 allEntries）。

    协作（SPEC C2.3）后详情缓存键按读者隔离（`{user_id}:{itinerary_id}`），编辑者
    写一次必须失效 owner 与全部成员的键，否则他人 10 分钟内读到旧内容。
    """
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        reader_ids = [
            row[0]
            for row in session.execute(
                select(ItineraryMember.user_id).where(ItineraryMember.itinerary_id == itinerary_id)
            ).all()
        ]
    if main is not None and main.user_id not in reader_ids:
        reader_ids.append(main.user_id)
    for reader_id in {*reader_ids, user_id}:
        cache_store.delete(DETAIL_CACHE_NAMESPACE, f"{reader_id}:{itinerary_id}")


def build_detail(user_id: int, itinerary_id: int) -> dict[str, Any]:
    """回源组装详情，**不读写缓存**。

    跨模块 API：`itinerary_version._write_snapshot` 用它取快照载荷。快照是在调用者
    **尚未提交**的事务里读的，若经 `detail()` 就会把未提交状态写进按用户缓存 600s 的
    详情表 —— 外层事务一旦回滚（唯一键冲突、锁等待超时），用户的
    `/api/itinerary/{id}` 会在接下来 10 分钟里显示一笔并不存在的修改。
    """
    main = find_readable_main(user_id, itinerary_id)
    with session_scope() as session:
        role = _member_role(session, itinerary_id, user_id)
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        # 一次查全部点位再按天分组：按天循环查询会产生 N+1
        items = (
            session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == itinerary_id)
                .order_by(ItineraryItem.day_id, ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        )
        budgets = session.execute(select(BudgetDetail).where(BudgetDetail.itinerary_id == itinerary_id)).scalars().all()

    items_by_day: dict[int, list[ItineraryItem]] = defaultdict(list)
    for item in items:
        items_by_day[item.day_id].append(item)
    intro_by_name: dict[str, ItineraryItem] = {}
    for item in items:
        if item.poi_name and item.poi_name not in intro_by_name:
            intro_by_name[item.poi_name] = item

    day_list: list[dict[str, Any]] = []
    for day in days:
        payload = {
            "dayId": day.id,
            "dayNo": day.day_no,
            # 天级生成态（PENDING/RUNNING/SUCCEEDED/FAILED）：刷新/断线轮询时
            # 前端据此按天显示「安排中」，不必依赖 SSE 事件流的连续性
            "generationStatus": day.generation_status,
            "travelDate": _iso_date(day.travel_date),
            "note": day.note,
            "theme": None,
            "miniRoute": None,
            "backupPlan": None,
            "photoSpots": None,
            "practicalNotes": None,
            "dayOptions": None,
            "items": [item_vo(item, intro_by_name) for item in items_by_day.get(day.id, [])],
        }
        metadata = _loads_object(day.metadata_json) or {}
        payload["theme"] = metadata.get("theme")
        payload["miniRoute"] = _as_map(metadata.get("miniRoute"))
        payload["backupPlan"] = _as_map_list(metadata.get("backupPlan"))
        payload["photoSpots"] = _as_map_list(metadata.get("photoSpots"))
        payload["practicalNotes"] = _as_str_list(metadata.get("practicalNotes"))
        # dayOptions 必须透出：metadata 已落库，不透出即前端槽位空转
        payload["dayOptions"] = _as_map_list(metadata.get("dayOptions"))
        day_list.append(payload)

    total_amount = sum((b.amount or Decimal("0") for b in budgets), Decimal("0"))
    pending_facts = sum(
        1
        for item in items
        if (item.review_requirement is None or item.review_requirement != "none") or item.freshness_status == "stale"
    )
    narrative_mismatches = _narrative_hotel_mismatches(main.plan_note, main.trip_theme, items)
    budget_warning = (
        f"当前估算总额 ￥{total_amount:.2f} 超出预算 ￥{main.budget:.2f}，"
        f"超出 ￥{total_amount - main.budget:.2f}；请调整行程或预算，出发前核实价格"
        if main.budget is not None and main.budget > 0 and total_amount > main.budget
        else None
    )
    quality_status = _quality_status(main.status, items, days, pending_facts, narrative_mismatches, budget_warning)
    issues = _quality_issues(quality_status, days, items, pending_facts, narrative_mismatches, budget_warning)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": main.id,
        "title": main.title,
        "city": main.city,
        "startDate": _iso_date(main.start_date),
        "endDate": _iso_date(main.end_date),
        "days": main.days,
        # 住宿晚数以主表为准；按"有酒店项的天数"反推会少算退房那一晚
        "stayNights": main.stay_nights if main.stay_nights is not None else max(len(day_list) - 1, 0),
        "persons": main.persons,
        "budget": _num(main.budget),
        "preferences": main.preferences,
        "hotelTier": main.hotel_tier,
        # L14：出发地（用户建行程时填）——前端据此显示"从 X 出发"与"查实时价"入口
        "originCity": main.origin_city,
        "status": main.status,
        "planNote": main.plan_note,
        "tripTheme": main.trip_theme,
        # S1：封面快照 / 收藏 / 归档 / 分享。shareToken **只回给 owner**：详情面
        # 对协作者（viewer/editor）也可读，而拿到 token 就等于拿到匿名可访问的
        # 公开分享链接——`GET /{id}/share` 刻意是 owner-only，这里不能绕过它。
        # 详情缓存按 user 分键（`detail` 的 cache_key），逐人裁剪不会互相污染。
        "coverUrl": main.cover_url,
        "coverSource": main.cover_source,
        "coverCredit": _loads_object(main.cover_credit),
        "favorite": bool(main.favorite),
        "archived": bool(main.archived),
        "shareToken": main.share_token if main.user_id == user_id else None,
        # 模板（C2.4）：owner 详情里可见发布状态（menu 出「发布/下架」）
        "templatePublishedAt": iso_datetime(main.template_published_at),
        "destinationStatus": _destination_status(items),
        "qualityStatus": quality_status,
        "qualityRuleVersion": QUALITY_RULE_VERSION,
        "validatedAt": None,
        "pendingFactCount": pending_facts,
        "qualityReport": _quality_report(quality_status, issues, pending_facts),
        "sources": _source_records(items),
        "suggestions": _loads_suggestions(main.suggestions_json),
        # 航班报价槽位（L13 契约先行，L14 填充）：无报价时为空列表，前端
        # QuoteStrip 整条不渲染。键常在、值常为空，消费方只判长度。
        "flightQuotes": _loads_flight_quotes(main.flight_quotes),
        "dayList": day_list,
        "budgetList": [{"category": b.category, "amount": _num(b.amount), "itemCount": b.item_count} for b in budgets],
        "totalAmount": float(total_amount),
        # 协作（C2.3）：调用者在本行程里的角色——前端据此隐藏写控件（后端闸门为准）
        "myRole": "owner" if main.user_id == user_id else (role or "viewer"),
    }


def item_vo(item: ItineraryItem, intro_by_name: dict[str, ItineraryItem]) -> dict[str, Any]:
    return {
        "id": item.id,
        "itemType": item.item_type,
        "poiName": item.poi_name,
        "poiId": item.poi_id,
        "address": item.address,
        "latitude": _num(item.latitude),
        "longitude": _num(item.longitude),
        "startTime": _iso_time(item.start_time),
        "endTime": _iso_time(item.end_time),
        "durationMin": item.duration_min,
        "cost": _num(item.cost),
        "tag": item.tag,
        "remark": item.remark,
        "whyThis": item.why_note,
        "openTime": item.open_time,
        "image": item.image_url,
        "imageUrl": item.image_url,
        "source": item.source,
        "sourceUpdatedAt": _iso_datetime(item.source_updated_at),
        "verificationStatus": item.verification_status,
        "valueKind": item.value_kind,
        "freshnessStatus": item.freshness_status,
        "reviewRequirement": item.review_requirement,
        "factEvidenceJson": item.fact_evidence_json,
        "factEvidence": _loads_object(item.fact_evidence_json),
        "intro": intro_by_name[item.poi_name].intro if item.poi_name in intro_by_name else None,
        "description": item.intro,
        "sortNo": item.sort_no,
    }


def _quality_status(
    status: int | None,
    items: list[ItineraryItem],
    days: list[ItineraryDay],
    pending_facts: int,
    narrative_mismatches: list[str] | None = None,
    budget_warning: str | None = None,
) -> str:
    has_failed_day = any(d.generation_status in _FAILED_DAY_STATUSES for d in days)
    has_incomplete_day = any(d.generation_status in _INCOMPLETE_DAY_STATUSES for d in days)
    has_stale = any(item.freshness_status == "stale" for item in items)
    if status == 3 or has_failed_day:
        return "BLOCKED"
    if has_incomplete_day or status == 1:
        return "DRAFT"
    if not items:
        return "BLOCKED"
    if has_stale:
        return "STALE"
    # 叙事层与条目实体矛盾（planNote 提到的酒店 ≠ 实际住宿）与待复核事实同级：
    # 条目本身可用，但"以 XX 为据点"这类引导若指错酒店会误导出发安排
    if narrative_mismatches or pending_facts > 0 or budget_warning:
        return "READY_WITH_WARNINGS"
    return "READY"


#: 行程说明/主题里的「XX酒店/饭店/宾馆/民宿/客栈」提及（2 字以上专名前缀）。
#: 泛指（"住在酒店""回酒店休息"）无前缀或前缀仅 1 字，天然不命中。
_HOTEL_MENTION_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9·]{2,20}?)(酒店|饭店|宾馆|民宿|客栈)")
#: 专名前缀里的修饰词与动词/介词（"以/在/订了/住在/旁边的…"）：剥掉后再比对，
#: 避免「以成都首座万豪酒店为据点」这类合法提及被"以"字拖成误报。迭代剥离。
_GENERIC_STEM_PREFIX_RE = re.compile(
    r"^(?:旁边的?|附近的?|对面的?|隔壁的?|那家|这家|一家|某家|另一家|本地|当地"
    r"|以|在|选了?|选定|订了?|入住了?|住进了?|住在?|回到?|去了?|进了?|换到?)"
)


def _clean_hotel_stem(raw: str) -> str:
    stem = raw
    while True:
        stripped = _GENERIC_STEM_PREFIX_RE.sub("", stem, count=1)
        if stripped == stem:
            return stem
        stem = stripped


def _narrative_hotel_mismatches(
    plan_note: str | None, trip_theme: str | None, items: Sequence[ItineraryItem]
) -> list[str]:
    """planNote/tripTheme 提到的具体酒店 vs 实际住宿条目的一致性（2026-09-30 评审遗留）。

    评审实录：planNote 写「博舍酒店为据点」，实际条目是「成都首座万豪酒店」——
    叙事在引导用户按错误的酒店安排出发。只做廉价双向包含比对（LLM 改写是
    生成侧的事，读模型不付 LLM 成本）；泛指提及与纯前缀修饰不误报。
    返回值 = 未能对应上任何住宿条目的提及（剥修饰词后，如「博舍酒店」）。
    """
    hotel_names = [item.poi_name for item in items if item.item_type == "hotel" and item.poi_name]
    if not hotel_names:
        return []
    text = " ".join(part for part in (plan_note, trip_theme) if part)
    if not text.strip():
        return []
    mismatches: list[str] = []
    for match in _HOTEL_MENTION_RE.finditer(text):
        stem = _clean_hotel_stem(match.group(1))
        if len(stem) < 2:
            continue
        mention = f"{stem}{match.group(2)}"
        if any(stem in name or name in mention for name in hotel_names):
            continue
        if mention not in mismatches:
            mismatches.append(mention)
    return mismatches


def _quality_issues(
    quality_status: str,
    days: list[ItineraryDay],
    items: list[ItineraryItem],
    pending_facts: int,
    narrative_mismatches: list[str] | None = None,
    budget_warning: str | None = None,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if quality_status == "BLOCKED":
        if not items:
            issues.append({"code": "NO_ITINERARY_ITEMS", "message": "没有可交付的行程地点"})
        if any(d.generation_status in _FAILED_DAY_STATUSES for d in days):
            issues.append({"code": "DAY_GENERATION_FAILED", "message": "至少一天的行程生成失败"})
    if narrative_mismatches:
        named = "、".join(narrative_mismatches[:3])
        issues.append(
            {
                "code": "NARRATIVE_HOTEL_MISMATCH",
                "message": f"行程说明提到的「{named}」与实际住宿条目不一致，请以行程条目为准",
            }
        )
    if pending_facts > 0 and quality_status != "BLOCKED":
        issues.append({"code": "FACT_REQUIRES_REVIEW", "message": f"{pending_facts} 项事实需要出发前复核"})
    if budget_warning:
        issues.append({"code": "BUDGET_EXCEEDED", "message": budget_warning})
    return issues


def _quality_report(quality_status: str, issues: list[dict[str, str]], pending_facts: int) -> dict[str, Any]:
    return {
        "qualityStatus": quality_status,
        "qualityRuleVersion": QUALITY_RULE_VERSION,
        "blockingIssues": issues if quality_status == "BLOCKED" else [],
        "warnings": issues if quality_status != "BLOCKED" else [],
        "metrics": {"pendingFactCount": pending_facts},
    }


def _destination_status(items: list[ItineraryItem]) -> str:
    """口径对齐 grounding_labels.is_authoritative_source（opentripmap/nominatim/web.search 前缀）：
    存在权威来源行 → researched；否则 draft_only。

    旧口径的失真（审查 P1-2）：流式链不写 llm.open_day，纯 LLM 草案会落进
    knowledge_backed——暗示已退役的 POI 库在背书，反向虚标。knowledge_backed
    值保留在契约枚举里不删（避免枚举变更），但本推导不再产出它。
    """
    if not items:
        return "draft_only"
    if any(is_authoritative_source(item.source) for item in items):
        return "researched"
    return "draft_only"


def _source_records(items: list[ItineraryItem]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for item in items:
        source = item.source
        if not source:
            continue
        record = records.setdefault(source, {"sourceId": source, "storageSource": source, "provider": source})
        if item.source_updated_at is not None and "retrievedAt" not in record:
            record["retrievedAt"] = _iso_datetime(item.source_updated_at)
    return list(records.values())


def _loads_suggestions(raw: str | None) -> list[dict[str, Any]]:
    if not raw or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("suggestions_json parse failed, returned [] instead")
        return []
    return value if isinstance(value, list) else []


def _loads_flight_quotes(raw: object) -> list[dict[str, Any]]:
    """往返报价列 → **线级形状**（camelCase）列表（L14）。

    该列存的是工具层产出的内部形状（snake_case，与 FlightQuote 字段同名）；
    详情 VO 是线级出口，必须过 FlightQuote 转成 camelCase 别名——否则前端按
    生成类型取 `deepLink` 会拿到 undefined（静默字段丢失，本模块 docstring
    开篇点名的老问题）。

    MySQL JSON 驱动可能给回已解析的 list 或字符串（方言/存量行），两种都收；
    形状不对的行整行丢弃——详情面宁可少一条报价，也不给前端一个会炸的类型。
    """
    if raw is None:
        return []
    value = raw
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        if not text.strip():
            return []
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            logger.warning("flight_quotes parse failed, returned [] instead")
            return []
    if not isinstance(value, list):
        return []
    quotes: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        shaped = shape_quote_for_wire(row)
        if shaped is None:
            logger.warning("flight_quotes row dropped: shape not matching FlightQuote")
            continue
        quotes.append(shaped)
    return quotes


def live_flight_quotes(
    user_id: int,
    itinerary_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """按需实时价（SerpApi google_flights）：读行程的出发地/目的地/日期窗 → 查询。

    **不落库**：这是"现在看一眼"，不是行程状态的变更——写进报价列会把生成时
    观测到的聚合价冲掉（那一列记的是"生成那一刻看到了什么"）。前端按响应渲染
    即可，刷新后回到行程自身的报价。

    错误边界（业务轨，无替代产出 → 抛 ApiError）：
    - 无出发地 / 出发地或目的地未映射 IATA → 400（这不是"查不到"，是查不了）；
    - 日期窗不全 → 400；
    - 配额尽 → 429（明确错误码，前端给"配额已用尽"文案）。
    其余缺席（未配 key / 上游无价）→ 200 + 空列表 + reason：问过了但没有，
    与"查不了"必须分开（同 places 的 None/[] 双语义）。
    """
    main = find_readable_main(user_id, itinerary_id)
    origin_city = str(getattr(main, "origin_city", None) or "").strip()
    if not origin_city:
        raise ApiError(400, "本行程没有出发地，无法查询航班实时价")
    window_start = start_date or main.start_date
    window_end = end_date or main.end_date
    if window_start is None or window_end is None:
        raise ApiError(400, "本行程缺少出发/返程日期，无法查询航班实时价")
    if live_quote_quota_exhausted():
        raise ApiError(429, "航班实时价查询配额已用尽，请稍后再试")
    quotes = search_live_flight_quotes(
        origin_city,
        str(main.city),
        window_start.isoformat(),
        window_end.isoformat(),
        limit=limit or 3,
    )
    return {
        "flightQuotes": quotes,
        "originCity": origin_city,
        "startDate": window_start.isoformat(),
        "endDate": window_end.isoformat(),
        "reason": None if quotes else "未查到该航线该日期的实时报价",
    }


def live_hotel_quotes(
    user_id: int,
    itinerary_id: int,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """按需查酒店实时价（LA2，SerpApi google_hotels）：读行程的城市/日期窗 → 查询。

    与航班侧 `live_flight_quotes` 同构：**不落库**（"现在看一眼"不是行程状态）；
    错误边界一致——城市/日期缺失 400（查不了），配额尽 429，问过但没有
    200 + 空列表 + reason。google_hotels 与 google_flights 共用同一只 SerpApi
    月配额计数器（250 次/月按 key 计），这里不做二次限额。
    """
    main = find_readable_main(user_id, itinerary_id)
    city = str(getattr(main, "city", None) or "").strip()
    if not city:
        raise ApiError(400, "本行程没有目的地城市，无法查询酒店实时价")
    window_start = start_date or main.start_date
    window_end = end_date or main.end_date
    if window_start is None or window_end is None:
        raise ApiError(400, "本行程缺少入住/离店日期，无法查询酒店实时价")
    if live_quote_quota_exhausted():
        raise ApiError(429, "实时价查询配额已用尽，请稍后再试")
    quotes = search_live_hotel_quotes(
        city,
        window_start.isoformat(),
        window_end.isoformat(),
        adults=int(getattr(main, "persons", 0) or 2),
        limit=limit or 5,
    )
    return {
        "hotelQuotes": quotes,
        "city": city,
        "checkIn": window_start.isoformat(),
        "checkOut": window_end.isoformat(),
        "reason": None if quotes else "未查到该城市该日期的实时酒店报价",
    }
