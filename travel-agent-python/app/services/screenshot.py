"""P3 业务入口；原图内存直传，短期结果按用户隔离，确认只返回待采用输入。"""

import io
import uuid
from datetime import UTC, datetime

from fastapi import UploadFile
from PIL import Image

from app.agent import extract_screenshot, resolve_screenshot_place, use_scene
from app.common import cache_store
from app.common.envelope import ApiError
from app.schemas.screenshot import (
    ScreenshotConfirmBody,
    ScreenshotDocumentVO,
    ScreenshotEntity,
    ScreenshotManualBody,
    ScreenshotResolveBody,
    ScreenshotResolveVO,
)
from app.services import cover_service, image_intent, llm_gateway_service, quota_service
from app.services.itinerary_city import guard_agent_call
from app.services.screenshot_confirmation import confirm_input

_DOCS = "screenshot-document"
_MATCHES = "screenshot-resolution"
_TTL = 15 * 60


def extract(user_id: int, file: UploadFile, kind: str) -> ScreenshotDocumentVO:
    if kind != "guide" and kind != "booking":
        raise ApiError(400, "截图类型应为攻略或预约")
    quota_service.enforce_llm_budget(user_id)
    data = cover_service.read_upload_capped(file.file, image_intent.IMAGE_INTENT_MAX_BYTES)
    uri = image_intent._to_data_uri(data)
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    with llm_gateway_service.route_scope(user_id), use_scene("assist"):
        result, model = guard_agent_call("截图识别服务暂不可用", lambda: extract_screenshot(uri, kind))
    identifier = uuid.uuid4().hex
    for index, entry in enumerate(result.entries):
        entry.record_id = str(index + 1)
    document = ScreenshotDocumentVO(
        **result.model_dump(),
        document_id=identifier,
        image_id=uuid.uuid4().hex,
        width=width,
        height=height,
        document_kind=kind,
        extracted_at=datetime.now(UTC).isoformat(),
        extractor=model,
    )
    cache_store.set_json(_DOCS, f"{user_id}:{identifier}", document.model_dump(mode="json", by_alias=True), _TTL)
    return document


def _document(user_id, identifier):
    raw = cache_store.get_json(_DOCS, f"{user_id}:{identifier}")
    if raw is None:
        raise ApiError(410, "截图结果已过期或不属于当前账号，请重新上传")
    return ScreenshotDocumentVO.model_validate(raw)


def resolve(user_id: int, body: ScreenshotResolveBody) -> ScreenshotResolveVO:
    document = _document(user_id, body.document_id)
    if not any(e.record_id == body.record_id for e in document.entries):
        raise ApiError(422, "截图记录不存在")
    # 仅按钮触发的一次点名查询；复用已有 1rps、UA、缓存与禁外网配置，不做自动补全。
    entry = next(e for e in document.entries if e.record_id == body.record_id)
    choices = resolve_screenshot_place(body.name, body.city, body.country_code, entry.kind)
    identifier = uuid.uuid4().hex
    status = "unavailable" if choices is None else "choices" if choices else "not_found"
    result = ScreenshotResolveVO(
        resolution_id=identifier,
        status=status,
        choices=choices or [],
        question="请核对地址、当地名称和具体馆区，明确选择；车站应选择对应车站实体。"
        if choices
        else "地点服务未返回可用实体；保留原字段，可改名后查询或手工录入，不认定地点不存在。",
    )
    cache_store.set_json(
        _MATCHES,
        f"{user_id}:{identifier}",
        {
            **body.model_dump(mode="json", by_alias=True),
            "choices": [c.model_dump(mode="json", by_alias=True) for c in result.choices],
        },
        _TTL,
    )
    return result


def manual(user_id: int, body: ScreenshotManualBody) -> ScreenshotResolveVO:
    document = _document(user_id, body.document_id)
    if not any(e.record_id == body.record_id for e in document.entries):
        raise ApiError(422, "截图记录不存在")
    identifier = uuid.uuid4().hex
    entity = ScreenshotEntity(
        entity_id=f"manual:{identifier}",
        name=body.name,
        aliases=[],
        address=body.address,
        country_code=body.country_code,
        latitude=body.latitude,
        longitude=body.longitude,
        identity_source="user_provided",
        category="user_input",
        entity_type=body.entity_type,
        match_reasons=["用户手工填写，未通过地图来源核验" if body.latitude is not None else "用户手工填写，坐标待定位"],
    )
    result = ScreenshotResolveVO(
        resolution_id=identifier,
        status="choices",
        choices=[entity],
        question="请确认手工地点；营业、票价与定位均不因此视为已核实。",
    )
    cache_store.set_json(
        _MATCHES,
        f"{user_id}:{identifier}",
        {**body.model_dump(mode="json", by_alias=True), "choices": [entity.model_dump(mode="json", by_alias=True)]},
        _TTL,
    )
    return result


def confirm(user_id: int, body: ScreenshotConfirmBody):
    document = _document(user_id, body.document_id)
    resolution = cache_store.get_json(_MATCHES, f"{user_id}:{body.resolution_id}")
    if not resolution or resolution["documentId"] != body.document_id or resolution["recordId"] != body.record_id:
        raise ApiError(410, "地点查询已过期或不属于这条截图，请重新查询")
    return confirm_input(body, document, resolution)
