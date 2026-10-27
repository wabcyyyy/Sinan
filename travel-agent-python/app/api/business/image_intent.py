"""图像意图理解端点：`POST /api/image-intent`（multipart：file + 可选 context 表单域）。

路由层只做一行转发（逻辑在 services/image_intent.py）；挂 `enforce_business_auth`
（不进 PUBLIC_PATHS，无匿名豁免）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.schemas.screenshot import ScreenshotConfirmBody, ScreenshotManualBody, ScreenshotResolveBody
from app.services import image_intent, screenshot

router = APIRouter(
    prefix="/api/image-intent",
    tags=["image-intent"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.post("")
def post_image_intent(
    file: UploadFile = File(...),
    context: str | None = Form(None),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    return ok(image_intent.interpret(user.id, file, context))


@router.post("/document")
def extract_document(
    file: UploadFile = File(...),
    kind: Literal["guide", "booking"] = Form(...),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    return ok(screenshot.extract(user.id, file, kind).model_dump(mode="json", by_alias=True))


@router.post("/resolve")
def resolve_document(body: ScreenshotResolveBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(screenshot.resolve(user.id, body).model_dump(mode="json", by_alias=True))


@router.post("/confirm")
def confirm_document(body: ScreenshotConfirmBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(screenshot.confirm(user.id, body).model_dump(mode="json", by_alias=True))


@router.post("/manual")
def manual_document(body: ScreenshotManualBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(screenshot.manual(user.id, body).model_dump(mode="json", by_alias=True))
