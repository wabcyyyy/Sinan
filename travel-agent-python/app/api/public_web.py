"""托管单实例入口：静态 SPA 同域交付，内部 Agent/MCP 不经过公网。"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.common import deep_health
from app.common.config import settings


class PublicSurface:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "").rstrip("/")
        if scope["type"] == "http" and any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in ("/api/agent", "/mcp", "/docs", "/redoc", "/openapi.json")
        ):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_routes(app: FastAPI) -> None:
    root = Path(settings.frontend_dist).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError("Frontend build is missing: FRONTEND_DIST must contain index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        ready = deep_health.snapshot()["status"] == "up"
        return JSONResponse({"status": "up" if ready else "degraded"}, status_code=200 if ready else 503)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or path.startswith(("api/", "s/")):
            raise HTTPException(404)
        if candidate.is_file():
            cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        if Path(path).suffix:
            raise HTTPException(404)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
