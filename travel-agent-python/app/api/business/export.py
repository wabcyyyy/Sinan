"""PDF 导出端点（对应 Java `ExportController`）。

`/download/{taskId}` 是唯一**不走 `Result` 信封**的响应：Java 返回的是
`ResponseEntity<Resource>`，前端用 `window.open`/a[download] 直接拿文件流，
包一层 JSON 反而会拿到一份"内容是 PDF 的 JSON"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path
from fastapi.responses import FileResponse

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.services import export_service

router = APIRouter(
    prefix="/api/export",
    tags=["export"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.post("/pdf/{itineraryId}")
def create_pdf(itineraryId: int = Path(..., ge=1), user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(export_service.create_pdf(user.id, itineraryId))


@router.get("/tasks/{taskId}")
def get_task(taskId: int = Path(..., ge=1), user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(export_service.get_task(user.id, taskId))


@router.get("/download/{taskId}")
def download(taskId: int = Path(..., ge=1), user: AuthUser = Depends(enforce_business_auth)) -> FileResponse:
    path = export_service.pdf_file(user.id, taskId)
    # filename 让 Starlette 自己发 `Content-Disposition: attachment`，不要再手写一遍
    return FileResponse(path, media_type="application/pdf", filename=f"itinerary_{taskId}.pdf")
