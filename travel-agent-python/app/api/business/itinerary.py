"""行程业务域：已迁移的端点集中在此（对应 Java ItineraryController）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, File, Header, Path, Query, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common import event_hub, event_publisher
from app.common.addons import require_addon
from app.common.config import settings
from app.common.envelope import ApiError, ok
from app.schemas.business.itinerary import (
    ApplyPlansBody,
    ArchiveBody,
    ChatEditBody,
    CoverBody,
    FavoriteBody,
    GenerateTripRequest,
    HotelOptionRequest,
    ItemUpsertRequest,
    LiveQuotesBody,
    NlEditBody,
    OptimizeDayBody,
    PreferenceSignalsBody,
    ShareCreateBody,
    UpdateDayBody,
    VersionBody,
)
from app.services import (
    cover_service,
    itinerary_chat,
    itinerary_city,
    itinerary_command,
    itinerary_events,
    itinerary_generation,
    itinerary_nl_edit,
    itinerary_plan_apply,
    itinerary_query,
    itinerary_version,
    preferences,
    quota_service,
    share_service,
)

router = APIRouter(
    prefix="/api/itinerary",
    tags=["itinerary"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/supported-cities")
def get_supported_cities() -> dict:
    return ok(itinerary_city.supported_cities())


# ---- agent 能力（同进程直调，迁移前是 Java→HTTP 一跳）----


@router.post("/clarify")
def post_clarify(body: dict[str, Any], user: AuthUser = Depends(enforce_business_auth)) -> dict:
    """槽位澄清：纯解析，不落库（同 Java `ItineraryCityService.clarify`）。"""
    quota_service.enforce_llm_budget(user.id)
    slots = body.get("slots")
    return ok(itinerary_city.clarify(user.id, str(body.get("message") or ""), slots if isinstance(slots, dict) else {}))


@router.post("/city-guide")
def post_city_guide(body: dict[str, Any], user: AuthUser = Depends(enforce_business_auth)) -> dict:
    quota_service.enforce_llm_budget(user.id)
    history = body.get("history")
    return ok(
        itinerary_city.city_guide(user.id, str(body.get("input") or ""), history if isinstance(history, list) else [])
    )


@router.post("/poi-nearby")
def post_poi_nearby(body: dict[str, Any], user: AuthUser = Depends(enforce_business_auth)) -> dict:
    quota_service.enforce_llm_budget(user.id)
    return ok(itinerary_city.poi_nearby(body))


@router.post("/generate")
def post_generate(
    body: GenerateTripRequest,
    user: AuthUser = Depends(enforce_business_auth),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict:
    """建壳 + 异步逐日生成，立即返回可轮询的初始详情（status=1）。

    `X-Idempotency-Key`（可选）：网络层重试 / 双击时带同一个键，TTL 内返回
    同一个行程而不是重复建壳（backlog「被重复请求咬过」）。
    """
    quota_service.enforce_llm_budget(user.id)
    return ok(itinerary_generation.generate(user.id, body, idempotency_key=x_idempotency_key))


# ---- 偏好：字面量路径必须声明在 /{id} 之前，否则会被路径参数吞掉 ----


@router.get("/preferences")
def get_preferences(user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(top_preferences(user.id))


@router.post("/preferences/signals")
def post_preference_signals(
    body: PreferenceSignalsBody,
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    preferences.record_signals(
        user.id,
        body.explicitPreferences,
        body.hardConstraints,
        body.negativePreferences,
        body.source,
        1.0 if body.confidence is None else body.confidence,
    )
    return ok()


@router.get("/preferences/signals")
def get_preference_signals(user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(preferences.signals(user.id, 50))


@router.get("")
def list_itineraries(
    view: str | None = Query(None, description="all|active|done|favorite|archived"),
    q: str | None = Query(None, description="按标题或城市模糊过滤"),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_query.list_summaries(user.id, view, q))


@router.get("/{id}")
def get_itinerary(
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_query.detail(user.id, id))


def top_preferences(user_id: int, limit: int = 5) -> list[str]:
    """高频正向偏好（Java 侧固定取 top 5）。"""
    return preferences.top_preferences(user_id, limit)


# ---- 写路径与版本（M4）----


@router.post("/{id}/items")
def add_item(
    request: ItemUpsertRequest,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_command.add_item(user.id, id, request))


@router.put("/items/{itemId}")
def update_item(
    request: ItemUpsertRequest,
    itemId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_command.update_item(user.id, itemId, request))


@router.post("/{id}/optimize")
def post_optimize(
    body: OptimizeDayBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
    _addon: None = Depends(require_addon("schedule_optimizer")),
) -> dict:
    """按路线重排某天（v2.6 W3）：确定性优化器，全量重排、不做锁定项、不删除点位。

    G-3.1：schedule_optimizer addon 停用时本端点 404（require_addon，隐藏而非 403）。
    """
    if body.dayId is None:
        raise ApiError(400, "dayId 必填")
    return ok(itinerary_command.optimize_day(user.id, id, body.dayId))


@router.patch("/{id}/days/{dayId}")
def patch_day(
    body: UpdateDayBody,
    id: int = Path(..., ge=1),
    dayId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """编辑日副标题（复用 metadata_json.theme；空串 = 清空回退派生标题）。"""
    return ok(itinerary_command.update_day(user.id, id, dayId, body.theme))


@router.post("/{id}/days/{dayNo}/regenerate")
def post_regenerate_day(
    id: int = Path(..., ge=1),
    dayNo: int = Path(..., ge=1),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    """重生成第 N 天（P0-3 部分交付一键自救）：真·LLM 重新生成，非 optimize 的确定性重排。

    只接 PENDING/FAILED/空天（已成功天 400）；同步执行，返回补齐后的最新详情
    （语义与配额拍板见 `itinerary_generation.regenerate_day` 与 `quota_service.enforce_llm_rate_only`）。
    配额硬线：只拦分钟窗、不扣日窗——用户不为补齐从未交付的天再付一次配额。
    """
    quota_service.enforce_llm_rate_only(user.id)
    return ok(itinerary_generation.regenerate_day(user.id, id, dayNo))


@router.post("/{id}/nl-edit")
def post_nl_edit(
    body: NlEditBody,
    id: int = Path(..., ge=1),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    """自然语言编辑：解析并**直接落库**（chat-edit 只出草稿，两者职责不同）。"""
    quota_service.enforce_llm_budget(user.id)
    return ok(itinerary_nl_edit.nl_edit(user.id, id, body.instruction))


@router.post("/{id}/apply-plans")
def post_apply_plans(
    body: ApplyPlansBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_plan_apply.apply_plans(user.id, id, body.actionMessageId, body.baseRevision))


@router.post("/{id}/hotel-option")
def post_hotel_option(
    body: HotelOptionRequest,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_plan_apply.apply_hotel_option(user.id, id, body))


@router.post("/{id}/quotes/live")
def post_live_quotes(
    body: LiveQuotesBody | None = Body(default=None),
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """按需查航班实时价（L14，SerpApi）。

    与生成链路解耦：**不落库**（见 `itinerary_query.live_flight_quotes` 的边界说明）。
    失败语义：查不了 → 400；配额尽 → 429；问过但没有 → 200 + 空列表 + reason。
    按用户分钟窗（审查 P1-8）：本端点直接消耗 SerpApi 的共享月池，先过闸再动手。
    """
    payload = body or LiveQuotesBody()
    quota_service.enforce_live_quote_budget(user.id)
    return ok(
        itinerary_query.live_flight_quotes(
            user.id,
            id,
            start_date=payload.startDate,
            end_date=payload.endDate,
            limit=payload.limit,
        )
    )


@router.post("/{id}/hotel-quotes/live")
def post_live_hotel_quotes(
    body: LiveQuotesBody | None = Body(default=None),
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """按需查酒店实时价（LA2，SerpApi google_hotels）。

    与航班侧同构：**不落库**；失败语义：查不了 → 400；配额尽 → 429（与
    google_flights 共用同一只月配额计数器）；问过但没有 → 200 + 空列表 + reason。
    按用户分钟窗（审查 P1-8）：与航班侧同一只共享月池，同一道闸。
    """
    payload = body or LiveQuotesBody()
    quota_service.enforce_live_quote_budget(user.id)
    return ok(
        itinerary_query.live_hotel_quotes(
            user.id,
            id,
            start_date=payload.startDate,
            end_date=payload.endDate,
            limit=payload.limit,
        )
    )


@router.delete("/items/{itemId}")
def delete_item(itemId: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(itinerary_command.delete_item(user.id, itemId))


@router.put("/{id}/days/{dayId}/order")
def reorder_items(
    # 请求体是裸 JSON 数组（Java `@RequestBody List<Long>`），不是 {"itemIds": [...]}
    item_ids: list[int],
    id: int = Path(..., ge=1),
    dayId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_command.reorder_items(user.id, id, dayId, item_ids))


@router.delete("/{id}")
def delete_itinerary(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    itinerary_command.delete_itinerary(user.id, id)
    return ok()


@router.get("/{id}/chat-history")
def chat_history(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(itinerary_chat.chat_history(user.id, id))


@router.delete("/{id}/chat-history")
def clear_chat_history(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    itinerary_chat.clear_history(user.id, id)
    return ok()


@router.get("/{id}/events")
async def events(
    id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)
) -> StreamingResponse:
    """订阅生成进度事件流（进程内 SSE；取代 Java 的「Redis pub/sub → SseEmitter」转发桥）。"""
    # 归属查询进线程池，登记订阅必须在事件循环里做（要捕获 loop 才能跨线程投递）
    main = await run_in_threadpool(itinerary_query.find_readable_main, user.id, id)
    subscription, _rejected = event_hub.subscribe(id, event_publisher.too_many_connections_envelope(id))
    try:
        snapshot = await run_in_threadpool(itinerary_events.terminal_snapshot, id, main)
    except BaseException:
        # 订阅已登记，但 `itinerary_events.event_frames` 的 finally 永远不会跑（生成器从未被进入）。
        # 漏这一次就少一个名额：MAX_SUBSCRIBERS_PER_ITINERARY=5，而终态行程不再广播，
        # 泄漏的队列不会自愈——5 次之后这条行程的进度流彻底打死到重启。
        event_hub.unsubscribe(subscription)
        raise
    return itinerary_events.sse_response(itinerary_events.event_frames(id, subscription, snapshot))


@router.post("/{id}/chat-edit")
def chat_edit(
    id: int = Path(..., ge=1), body: ChatEditBody = Body(...), user: AuthUser = Depends(enforce_business_auth)
) -> dict:
    quota_service.enforce_llm_budget(user.id)
    return ok(itinerary_chat.chat_edit(user.id, id, body.message, body.history))


@router.post("/{id}/chat-edit/stream")
async def chat_edit_stream(
    id: int = Path(..., ge=1), body: ChatEditBody = Body(...), user: AuthUser = Depends(enforce_business_auth)
) -> StreamingResponse:
    """对话编辑的 SSE 变体：与非阻塞版同参构造、同一条落库收尾路径。"""
    quota_service.enforce_llm_budget(user.id)
    await run_in_threadpool(itinerary_query.find_writable_main, user.id, id)
    return itinerary_events.sse_response(
        itinerary_events.sse_frames(itinerary_chat.chat_edit_stream(user.id, id, body.message, body.history))
    )


@router.get("/{id}/versions")
def list_versions(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(itinerary_version.list_versions(user.id, id))


@router.get("/{id}/versions/diff")
def versions_diff(
    fromVersionId: int = Query(..., ge=1),
    toVersionId: int = Query(..., ge=1),
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_version.diff(user.id, id, fromVersionId, toVersionId))


@router.post("/{id}/versions")
def create_version(
    body: VersionBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_version.create_snapshot(user.id, id, body.operation, body.summary))


@router.post("/{id}/versions/{versionId}/restore")
def restore_version(
    id: int = Path(..., ge=1),
    versionId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_version.restore(user.id, id, versionId))


# ---- 封面 / 收藏 / 归档（SPEC v2.3 §6.3/§6.5，S1）----


@router.post("/{id}/cover")
def set_cover(
    body: CoverBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """封面三来源：unsplash=服务端 snapshot 落盘；default=四个 cover 列置 NULL。"""
    if body.source == "unsplash":
        return ok(cover_service.set_cover_unsplash(user.id, id, body.unsplashId or ""))
    if body.source == "default":
        return ok(cover_service.set_cover_default(user.id, id))
    raise ApiError(400, "source 仅支持 unsplash|default")


@router.post("/{id}/cover/upload")
def upload_cover(
    file: UploadFile = File(...),
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """用户上传（jpeg/png，≤5MB——S0-3 在服务层流式截断）；文件名一律不使用（uuid 落盘）。"""
    data = cover_service.read_upload_capped(file.file, settings.cover_upload_max_bytes)
    return ok(
        cover_service.set_cover_upload(user.id, id, filename=file.filename, content_type=file.content_type, data=data)
    )


@router.post("/{id}/favorite")
def set_favorite(
    body: FavoriteBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_command.set_favorite(user.id, id, body.favorite))


@router.post("/{id}/archive")
def set_archived(
    body: ArchiveBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(itinerary_command.set_archived(user.id, id, body.archived))


# ---- 公开分享（SPEC v2.3 §6.6，S2）：owner 面三端点；匿名面在 share.py ----


@router.post("/{id}/share")
def create_share(
    body: ShareCreateBody,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """创建/轮换分享链接（重复创建会让旧链接立即失效）。"""
    return ok(share_service.create_share(user.id, id, body.expireDays))


@router.get("/{id}/share")
def get_share(
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(share_service.get_share(user.id, id))


@router.delete("/{id}/share")
def remove_share(
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    share_service.remove_share(user.id, id)
    return ok()
