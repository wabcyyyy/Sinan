"""分享卡外壳路由：`GET /s/{token}`（匿名可达，与 /api/share/{token} 同一套限速）。

为什么不是 JSON：这个端点服务的是微信/Twitter 等不执行 JS 的爬虫，返回的是
注入 og meta 后的 SPA 外壳 HTML（逻辑见 services/share_card.py）；对浏览器
用户它就是普通的首帧 HTML。取不到壳（未配置/边缘不可达）时 302 回首页，
绝不让分享链接 500。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from app.api.security import enforce_business_auth
from app.common.client_ip import client_ip
from app.services import share_card, share_service

router = APIRouter(
    prefix="/s",
    tags=["share-shell"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/{token}")
def shared_shell(token: str, request: Request) -> Response:
    # 与 view_shared 同闸：外壳端点同样打一次 DB，不能比数据端点更抗刷
    share_service.enforce_rate_limit(client_ip(request))
    page = share_card.shared_shell_html(token)
    if page is None:
        return RedirectResponse("/", status_code=302)
    return Response(
        content=page,
        media_type="text/html; charset=utf-8",
        # 外壳随构建变、og 随行程变：与 SPA index.html 同口径，禁止中间缓存
        headers={"Cache-Control": "no-cache"},
    )
