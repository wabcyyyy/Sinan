"""图像意图理解端点：`POST /api/image-intent`（multipart：file + 可选 context 表单域）。

路由层只做一行转发（逻辑在 services/image_intent.py）；挂 `enforce_business_auth`
（不进 PUBLIC_PATHS，无匿名豁免）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.services import image_intent

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
