"""FastAPI 应用入口。

职责：
- 组装 FastAPI 应用、挂载 CORS 与路由、定义启动生命周期。

实现要点：
- OpenAPI 只有一个生产者：`scripts/export_contracts.py` 导出 `contracts/openapi.json`
  （启动期不再落第二份，否则根目录产物与入仓产物会各说一套）；
- 生成任务的自动续跑扫描与两个有界池的优雅关闭也挂在同一个 lifespan 上；
- 按 settings.agent_cors_origins 配置跨域来源；
- 把 app.api.agent.router 挂载到 /api/agent 前缀；
- 本地以 uvicorn 运行 main:app（端口 8000，支持热重载）。

依赖：
- fastapi/uvicorn；app.api.agent；app.common.config；
  app.services.generation_recovery / itinerary_generation。
"""

import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import Message, Receive, Scope, Send

from app.agent.runtime import checkpoint
from app.agent.runtime.usage_store import usage_store
from app.api import agent, mcp
from app.api.business import business_routers
from app.api.security_headers import SecurityHeadersMiddleware
from app.common import cron, model_registry, operations, retention, shutdown, timezone_check
from app.common.config import settings
from app.common.envelope import fail, install_exception_handlers
from app.common.request_id import RequestIdMiddleware
from app.db import migrate as db_migrate
from app.services import (
    cost_ledger,
    export_service,
    generation_recovery,
    itinerary_care_check,
    itinerary_chat,
    itinerary_generation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

logger = logging.getLogger(__name__)

# ---- 请求体上限（P0-4 应用侧；边缘两处镜像见注释） ----
# 口径与 travel-frontend-react/nginx.conf 的 client_max_body_size 对齐：
# /api/** 全量 2MB（JSON API 足够）；
# multipart 上传端点按"服务侧文件本体上限 + 1MB 包裹余量"分档放行——服务层用
# cover_service.read_upload_capped 流式截断文件本体（封面/图片意图 5MB、语音 8MB），
# 全局一刀切 2MB 会把上传拦死在到达服务层之前。边缘与本中间件
# 谁先拦都是 413。
API_BODY_MAX_BYTES = 2 * 1024 * 1024
# 6MB 档：封面上传（cover_upload_max_bytes=5MB）与图片意图（IMAGE_INTENT_MAX_BYTES=5MB）。
UPLOAD_BODY_MAX_BYTES = 6 * 1024 * 1024
# 9MB 档：语音意图（SPEECH_INTENT_MAX_BYTES=8MB，16k 单声道 WAV 较长录音常见 2-8MB）。
SPEECH_UPLOAD_BODY_MAX_BYTES = 9 * 1024 * 1024

_ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# 6MB 档的固定上传端点（封面上传是带数字段的模式路径，见 _is_cover_upload）。
_UPLOAD_6MB_PATHS = frozenset({"/api/image-intent"})
_SPEECH_INTENT_PATH = "/api/speech-intent"


def _is_cover_upload(path: str) -> bool:
    """/api/itinerary/{id}/cover/upload（id 为数字段）——与边缘 matcher 的
    ^/api/itinerary/[^/]+/cover/upload$（nginx）
    同一命中面。"""
    prefix, suffix = "/api/itinerary/", "/cover/upload"
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return False
    return path[len(prefix) : -len(suffix)].isdigit()


def _body_limit(path: str) -> int:
    """按路径返回请求体上限档，与边缘 matcher 的命中面互为镜像：
    封面上传/图片意图 6MB、语音意图 9MB、其余 /api/** 一律 2MB。"""
    if path in _UPLOAD_6MB_PATHS or _is_cover_upload(path):
        return UPLOAD_BODY_MAX_BYTES
    if path == _SPEECH_INTENT_PATH:
        return SPEECH_UPLOAD_BODY_MAX_BYTES
    return API_BODY_MAX_BYTES


def _content_length(scope: Scope) -> int | None:
    """从 ASGI headers 取 Content-Length；缺失或不可解析返回 None（chunked 由边缘兜底）。"""
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class RequestBodySizeLimitMiddleware:
    """Content-Length 超限直接拒绝；业务读取 body 时再累计限制 chunked/虚报长度。

    托管入口可直接到 FastAPI，不能依赖 nginx 为无 Content-Length 请求兜底。
    不额外缓冲请求体，也不包装响应流，GET/SSE 保持流式交付。
    """

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/"):
            limit = _body_limit(path)
            length = _content_length(scope)
            if length is not None and length > limit:
                body = json.dumps(fail(413, "请求体过大，请压缩后重试"), ensure_ascii=False).encode("utf-8")
                response: Message = {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
                await send(response)
                await send({"type": "http.response.body", "body": body})
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > _body_limit(path):
                    raise HTTPException(413, "请求体过大，请压缩后重试")
            return message

        await self._app(scope, limited_receive if path.startswith("/api/") else receive, send)


def _cleanup_usage() -> None:
    """清理保留期之外的 LLM 用量明细（默认保留 90 天）；失败只告警（cron 会记）。"""
    removed = usage_store.cleanup(90 * 86400)
    if removed:
        logger.info("[usage] cleaned %d rows older than 90d", removed)


def _cleanup_checkpoints() -> None:
    """清理保留期之外的图检查点 thread（PR-3；LangGraph 官方告警：checkpoints 无限增长需定期清理）。"""
    removed = checkpoint.cleanup_old_threads(7 * 86400)
    if removed:
        logger.info("[checkpoint] cleaned %d threads older than 7d", removed)


def _sweep_cost_reservations() -> None:
    """崩溃收口：到期仍 OPEN 的模型预留转 ABANDONED（V17/T2）。

    没有这一步，"进程死在半路的那笔预留"会永久占着 `cost_max_open_reservations` 的格子，
    预留生命周期只剩一半——PLAN §5 T2 要的"崩溃后未结算请求有界"落不了地。
    间隔刻意小于 `cost_reservation_ttl_seconds`（270s）：结算不回来时格子要按时腾出来。
    """
    abandoned = cost_ledger.sweep_expired_reservations()
    if abandoned:
        logger.info("[cost] abandoned %d expired reservation(s)", abandoned)


def _cleanup_traces() -> None:
    """轨迹文件按日归档 + 删过期归档（审查 P2-2：此前纯追加无上限）。"""
    removed = retention.rotate_trace_store(settings.trace_storage_path)
    if removed:
        logger.info("[trace] removed %d archives older than %dd", removed, retention.TRACE_ARCHIVE_KEEP_DAYS)


def _cleanup_exports() -> None:
    """导出文件保留 30 天（审查 P2-2：应用侧此前永不删除，还被 backup 连带放大）。"""
    removed = retention.cleanup_exports(settings.export_dir)
    if removed:
        logger.info("[export] removed %d files older than %dd", removed, retention.EXPORT_KEEP_DAYS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动期配置校验（G-1.5）：安全 fail-fast（非回环绑定必须配内部令牌、
    # JWT 密钥强度）收拢在 Settings.validate_boot；坏配置 RuntimeError →
    # uvicorn 以非 0 退出，避免"起来了但配置是坏的"。
    settings.validate_boot()
    if settings.trial_billing_enabled:
        from app.common import trial_billing

        trial_billing.recover_reservations()
    # 模型角色注册表（2026-10-04）：角色键写错（未知 provider / 缺模型名 / 指向没配
    # base_url 的 provider）在启动期一次性炸掉，不留到运行中第一次调用才以 404/超时现身。
    # 独立于 validate_boot 是为了不把"角色解析"耦合进 Settings（避免 config ↔ registry 互相 import）。
    try:
        model_registry.validate()
    except ValueError as exc:
        raise RuntimeError(f"模型角色配置非法：{exc}") from exc
    # 周期任务统一登记（G-3.3）：usage 清理每天一次、生成续跑 60s 一轮。
    # 两个任务都经 app.common.cron——pytest 环境自动 no-op，不再各写各的线程。
    cron.register("usage-cleanup", 86400, _cleanup_usage)
    cron.register("checkpoint-cleanup", 86400, _cleanup_checkpoints)
    cron.register("cost-reservation-sweep", 120, _sweep_cost_reservations, startup_delay_seconds=5)
    # 成本账本后端（V17/T2）：RunLimits 的 reserve/settle 事件要落 MySQL 的 cost_ledger。
    # 这一步原先只写在 cost_ledger 的 docstring 里、无人调用——不接上就是"账本实现了但
    # 线上根本不记"，费用硬顶永远读到空账。放启动期并且不吞异常：装不上就不要装作在服务。
    cost_ledger.install_backend()
    # 增长面保留策略（审查 P2-2）：轨迹文件归档、导出文件 30 天
    cron.register("trace-cleanup", 86400, _cleanup_traces, startup_delay_seconds=8)
    cron.register("export-cleanup", 86400, _cleanup_exports, startup_delay_seconds=8)
    # 时区对照（审查 P1-9）：僵尸恢复的 5 分钟窗口要求应用与 MySQL 同时区，
    # 此前只有注释没有防线。这里登记成启动后几秒跑一次的对照检查——走 cron
    # 而不是直接阻塞 lifespan，是不让"启动应用"依赖数据库可用（同上文 STARTUP
    # DELAY 的既有取舍），且 pytest 里自动 no-op 不影响离线套件。
    cron.register("timezone-check", 86400, timezone_check.verify, startup_delay_seconds=6)
    generation_recovery.register_loop()
    cron.register("operations-monitor", 60, operations.check, startup_delay_seconds=15)
    # 出发前照看扫描（个人 Agent V1，规格 §3.2）：每 5 分钟、单次最多 5 项、串行；
    # 执行与「立即检查」共用同一函数与争抢，全局开关关闭时扫描直接空转。
    cron.register(
        "itinerary-care-scan",
        itinerary_care_check.SCAN_INTERVAL_SECONDS,
        itinerary_care_check.scan_due_checks,
        startup_delay_seconds=12,
    )
    cron.start_all()
    try:
        yield
    finally:
        # 滚动发布时不先把在跑的生成切掉：先停周期任务，再等在跑的任务收尾（上限 30s），
        # 最后才关池。
        cron.stop_all()
        await shutdown.drain(
            (
                itinerary_generation.generation_pool,
                itinerary_generation.enricher_pool,
                export_service.export_pool,
                itinerary_chat.chat_pool,
            )
        )


app = FastAPI(title="travel-agent-python", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.agent_cors_origins.split(",") if origin.strip()],
    # 业务接口以 HttpOnly Cookie(TA_AUTH) 为主凭据，跨源时必须允许携带凭据，
    # 否则浏览器不会带上 Cookie（Java 侧 CorsConfiguration 亦为 allowCredentials=true）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_exception_handlers(app)
# 请求体上限预检（P0-4 应用侧，上限常量见文件头注释：与边缘 Caddy/nginx 两处镜像）。
# 挂在 SecurityHeadersMiddleware 之内（后 add 的在外层）：413 响应同样带上安全响应头。
app.add_middleware(RequestBodySizeLimitMiddleware)
if settings.serve_frontend:
    from app.api.public_web import PublicSurface

    app.add_middleware(PublicSurface)
# 全站安全响应头（R1-3）：纯 ASGI 包装，只补头、不缓冲 SSE
app.add_middleware(SecurityHeadersMiddleware)
# 全站请求关联 id（审计 §3.5.1/P0-7）：每请求生成短 id → scope state + X-Request-ID
# 响应头，envelope 的 500 兜底日志/响应头带同一 id。最后 add = 最外层：RequestBodySizeLimit
# 就地短路的 413 响应同样带 id。500 兜底响应经 ServerErrorMiddleware（在用户中间件之外）
# 直发，不经过这里的 send 包装，因此那个头由 envelope._handle_unexpected 就地补。
app.add_middleware(RequestIdMiddleware)

app.include_router(agent.router, prefix="/api/agent", tags=["agent"])

# MCP 只读工具出口（G-3.6）：addon 门控（默认关）+ AGENT_INTERNAL_TOKEN 鉴权，
# 两者都在 McpGate 里按请求实时判定——addon 关闭时整个前缀 404。
app.mount("/mcp", mcp.mcp_asgi_app())
# 迁移自 Java 的业务域：router 自带完整 /api/... 前缀，便于按路径前缀灰度切流
for business_router in business_routers:
    app.include_router(business_router)

if settings.serve_frontend:
    from app.api.public_web import install_routes

    install_routes(app)


if __name__ == "__main__":
    # 启动即迁移（等价 Java 侧 Flyway）：空库建表、既有库只打点。放在这里而不是 lifespan，
    # 是为了让测试与 import 永不触发真库连接；失败即拒绝启动，避免服务"起来了但表是空的"。
    try:
        db_migrate.ensure_schema()
    except Exception as exc:
        logging.getLogger(__name__).error("schema migration failed: %s", exc)
        raise
    uvicorn.run(
        "main:app",
        host=settings.agent_host,
        port=settings.port,
        reload=settings.agent_reload,
        proxy_headers=False,
        timeout_graceful_shutdown=5,
    )
