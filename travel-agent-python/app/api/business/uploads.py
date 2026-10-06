"""上传文件访问（SPEC v2.3 §6.4）：匿名可读（PUBLIC_PATHS 已放行）、防穿越、分档缓存。

**不挂 StaticFiles**：自定义路由才能控三件事——① 路径规范化（resolve 后必须仍在
uploads_dir 内）；② 禁目录列表（只服务文件）；③ 404 返回 PlainText（`<img>` 只看
状态码，走 ApiError 信封反而把静态 miss 与业务 404 混成一种形状）。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse, PlainTextResponse

from app.api.security import enforce_business_auth
from app.common.config import settings

router = APIRouter(
    prefix="/api/uploads",
    tags=["uploads"],
    dependencies=[Depends(enforce_business_auth)],
)

# provider snapshot 的名字是 sha256(ref)[:16].jpg（可 immutable）；uuid 名不可
_HASH_NAME = re.compile(r"^[0-9a-f]{16}\.jpg$")


def resolve_upload_path(relative: str) -> Path | None:
    """把 URL 相对路径解析为磁盘文件；越界（`..`/绝对路径/符号链接逃逸）一律 None。"""
    base = Path(settings.uploads_dir).resolve()
    try:
        candidate = (base / relative).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    if candidate != base and base not in candidate.parents:
        return None
    return candidate


def cache_control_for(name: str) -> str:
    if _HASH_NAME.match(name):
        return "public, max-age=604800, immutable"
    return "public, max-age=3600"


@router.get("/{path:path}")
def get_upload(path: str) -> Response:
    resolved = resolve_upload_path(path)
    if resolved is None:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(resolved, headers={"Cache-Control": cache_control_for(resolved.name)})
