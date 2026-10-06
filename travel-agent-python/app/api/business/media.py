"""图片域端点：POI 实景图解析 + 同源图片代理。

原先挂在 `/api/amap` 前缀下（Java `AmapController` 的移植）；去高德后改名——
URL 里不该再出现一个已经不存在的供应商。两个端点的语义与降级行为不变：
`poi-photo` 302 到同源代理，未命中 404；`image-proxy` 只代取白名单图床。

两者都在 `PUBLIC_PATHS` 里匿名可达，因此各挂一道按 IP 的闸门（R1-4）：
`poi-photo` 未命中一次最多打 6 个上游请求，没有闸门就是一个脚本耗尽出网配额。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.security import enforce_business_auth
from app.common.client_ip import client_ip
from app.services import image_proxy, poi_photo, rate_limit

router = APIRouter(
    prefix="/api",
    tags=["media"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/image-proxy")
def image(request: Request, url: str = Query(...)) -> Response:
    rate_limit.enforce_public_bucket("image-proxy", client_ip(request))
    return image_proxy.proxy_image(url)


@router.get("/poi-photo")
def poi_photo_redirect(request: Request, name: str = Query(...), city: str = Query(...)) -> Response:
    """解析点位实景图后 302 到同源图片代理（维基/图库链，未命中 404）。"""
    rate_limit.enforce_public_bucket("poi-photo", client_ip(request))
    if not name.strip() or len(name) > 64 or not city.strip() or len(city) > 32:
        return Response(status_code=400)
    photo_url = poi_photo.resolve_poi_photo(name, city)
    if not photo_url:
        return Response(status_code=404)
    return RedirectResponse(url=f"/api/image-proxy?url={quote(photo_url, safe='')}", status_code=302)
