"""语音意图理解端点：`POST /api/speech-intent`（multipart：file）。

路由层只做一行转发（逻辑在 services/speech_intent.py）；挂 `enforce_business_auth`
（与 image-intent 同口径，不进 PUBLIC_PATHS，无匿名豁免）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.services import speech_intent

router = APIRouter(
    prefix="/api/speech-intent",
    tags=["speech-intent"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.post("")
def post_speech_intent(
    file: UploadFile = File(...),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    return ok(speech_intent.interpret(user.id, file))
