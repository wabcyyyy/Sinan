"""业务路由聚合（Java → Python 迁移的落点）。

约定：各 router 自带完整 `/api/...` 前缀，main.py 挂载时不再加前缀——这样
双跑期可以按**路径前缀**在 vite/nginx 上逐个域切流量（PLAN v3.0 §2），
而不是靠重写前缀维持。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.business.admin import router as admin_router
from app.api.business.atlas import router as atlas_router
from app.api.business.auth import auth_router, user_router
from app.api.business.client_errors import router as client_errors_router
from app.api.business.covers import router as covers_router
from app.api.business.expenses import router as expenses_router
from app.api.business.export import router as export_router
from app.api.business.feedback import router as feedback_router
from app.api.business.image_intent import router as image_intent_router
from app.api.business.itinerary import router as itinerary_router
from app.api.business.llm_gateway import router as llm_gateway_router
from app.api.business.media import router as media_router
from app.api.business.members import router as members_router
from app.api.business.pois import router as pois_router
from app.api.business.probe import router as probe_router
from app.api.business.share import router as share_router
from app.api.business.share_shell import router as share_shell_router
from app.api.business.speech_intent import router as speech_intent_router
from app.api.business.templates import router as templates_router
from app.api.business.uploads import router as uploads_router
from app.api.business.weather import router as weather_router

business_routers: tuple[APIRouter, ...] = (
    auth_router,
    user_router,
    client_errors_router,
    itinerary_router,
    llm_gateway_router,
    image_intent_router,
    speech_intent_router,
    covers_router,
    expenses_router,
    feedback_router,
    members_router,
    pois_router,
    weather_router,
    media_router,
    export_router,
    admin_router,
    probe_router,
    uploads_router,
    share_router,
    share_shell_router,
    atlas_router,
    templates_router,
)
