"""旅行规划 Agent 的 HTTP 入口层。

职责：
- 定义 FastAPI 路由，把前端请求转换为对 app.agent 各子模块的函数调用；
- 统一用 ApiResponse 包裹返回结果，并对生成类接口做内部 token 鉴权；
- 把 Pydantic 模型与 agent 层返回值做适配（如 chat-turn、edit-ops 的字段清洗）。

实现要点：
- 写库/生成类接口（generate、adjust、clarify、chat-turn 等）通过 require_internal_token
  依赖校验 X-Agent-Token，未配置时放行；
- edit-ops 返回原始 snake_case 字典，避免 WireModel 的 camel 别名影响跨语言消费方；
- 业务编排逻辑一律下沉到 app.agent.*，本文件只做协议适配与鉴权。

依赖：
- app.agent：各业务子模块；app.schemas：请求/响应模型；app.common.config：鉴权配置。
"""

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import ValidationError

from app.agent import (
    find_nearby_pois,
    metrics,
    observe_run,
    registry,
    run_adjust,
    run_butler_note,
    run_chat_turn,
    run_city_guide,
    run_clarify,
    run_edit_ops,
    run_generate,
    run_generate_day,
    run_generate_trip_stream,
    run_local_replan,
    run_plan_context,
    run_poi_intros,
    scene,
    usage_store,
    use_scene,
)
from app.common import deep_health
from app.common.config import settings
from app.schemas.agent_ops import (
    ButlerNoteRequest,
    ButlerNoteResponse,
    CityGuideRequest,
    CityGuideResponse,
    PoiIntrosRequest,
    PoiIntrosResponse,
    PoiNearbyItem,
    PoiNearbyRequest,
    PoiNearbyResponse,
)
from app.schemas.common import ApiResponse
from app.schemas.stream_events import ErrorEvent, StartEvent, to_wire
from app.schemas.trip import (
    AdjustRequest,
    AdjustResponse,
    ChatTurnRequest,
    ChatTurnResponse,
    ClarifyRequest,
    ClarifyResponse,
    DailyPlan,
    EditOpRequest,
    GenerateDayRequest,
    GenerateRequest,
    GenerateResponse,
    LocalReplanRequest,
    PlanContextRequest,
)

# 全 router 前置依赖：无——`/hello` 与 `/health` 必须匿名可达（Dockerfile
# HEALTHCHECK 在拿到令牌前就探活），所以闸门只能挂在**需要令牌的那些端点**上，
# 不能挂到 router 级。落地方式：把速率桶并进 `require_internal_token` 本身
# （见下方说明），这样所有 /api/agent/v1/** 端点既认人也限速，探活端点两不受。
router = APIRouter()

logger = logging.getLogger(__name__)

# 整段流式事件队列上限：事件数约 = 2 + 2×天数，256 留足余量；
# 队列满时生产者等待（消费端慢 = 背压），取消后立即放弃投递。
_STREAM_QUEUE_MAXSIZE = 256
# 队列空闲等待轮询粒度：也是取消后 worker/消费端的最大响应时延
_STREAM_POLL_SECONDS = 0.5


async def _bridge_worker_events(producer: Callable[[threading.Event], Iterator[dict]]) -> AsyncIterator[str]:
    """worker 线程 → 异步响应生成器 的桥接（有界队列 + 断连即取消）。

    生成跑在独立 worker 线程（contextvars 完整，见 generate-stream docstring），
    事件经有界队列转交本生成器。客户端断开时 Starlette 取消本生成器任务，
    CancelledError 穿过 finally → 置 cancel 广播给 worker：不再发起新 LLM 调用，
    在途流式请求由 llm_client 行级掐断（成本止损）。

    队列语义：满则 worker 阻塞等待（背压）；取消后不再投递；哨兵保证正常收尾。
    取消后不使用无界 get：空闲等待有 0.5s 上界，任务取消后线程池线程不被占用。
    """
    events: queue.Queue = queue.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
    cancel = threading.Event()
    sentinel = object()

    def _put(item: object) -> bool:
        while not cancel.is_set():
            try:
                events.put(item, timeout=_STREAM_POLL_SECONDS)
                return True
            except queue.Full:
                continue
        return False

    def _run() -> None:
        try:
            for event in producer(cancel):
                # 取消后事件不再投递；生成端会在下个检查点自行退出
                _put(event)
        except Exception:
            logger.exception("generate-stream producer crashed")
        finally:
            _put(sentinel)

    # INV-8 豁免（2026-10-05 拍板：注记+期限，不换有界池）：单 worker 线程是保
    # contextvars 的必要取舍（本端点 docstring 详释，task_pool 通道会把它丢进线程池
    # 逐次 next() 而碎掉上下文）；并发面已有 require_internal_token 门禁 + 前端零
    # 消费兜底。期限：本端点接入多租户/对外暴露前必须换成有界通道并补并发上限。
    threading.Thread(target=_run, name="trip-stream-worker", daemon=True).start()
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                item = await loop.run_in_executor(None, functools.partial(events.get, timeout=_STREAM_POLL_SECONDS))
            except queue.Empty:
                continue
            if item is sentinel:
                break
            yield json.dumps(item, ensure_ascii=False) + "\n"
    finally:
        # 客户端断开（任务取消）或正常收尾：向 worker 广播取消
        cancel.set()


def _fail_payload(exc: Exception, endpoint: str) -> ApiResponse[None]:
    """把异常转成对外安全的错误信封。

    非 ValueError（AttributeError/KeyError/pydantic ValidationError 等）的原文
    可能包含字段校验细节、SQL 片段或第三方报错，直接透传等于泄露内部实现；
    这里完整记日志，对外只给通用文案。ValueError 是业务层有意抛出的可读降级
    原因（如"开放研究失败"），保留但裁剪长度。
    """
    logger.exception("[%s] request failed: %s", endpoint, exc)
    # ValidationError 是 ValueError 子类，但其原文含字段级校验细节，不外泄。
    if isinstance(exc, ValidationError):
        return ApiResponse.fail("请求参数不合法", code=400)
    # JSONDecodeError 也是 ValueError 子类，但它是"上游给了坏数据"而非业务降级原因：
    # 上游响应体可能被打进消息里，不属于"对用户如实降级"的那一类（R2-9）。
    if isinstance(exc, json.JSONDecodeError):
        return ApiResponse.fail("服务暂时不可用，请稍后重试", code=502)
    if isinstance(exc, ValueError):
        return ApiResponse.fail(str(exc)[:200], code=500)
    return ApiResponse.fail("服务暂时不可用，请稍后重试", code=500)


def require_internal_token(request: Request, x_agent_token: str | None = Header(default=None)) -> None:
    """内部令牌校验：配了就必须对，比较走常数时间（R1-8）。外加一层进程内速率桶。

    没配令牌时是否放行由**部署形态**决定，不在这里判：`Settings.validate_boot` 已经
    拒绝"绑定非回环地址但不设令牌"（那等于匿名烧钱接口），所以"空令牌"只可能出现在
    回环直调的本地/测试拓扑。真正把它带到公网的是边缘——nginx 必须以 `deny all`
    丢掉 `/api/agent/`，见 `travel-frontend-react/nginx.conf`。

    速率桶（审查 P1-8）：token 一旦泄露，调用方可以无限打（单请求有 RunLimits 封顶，
    但"打多少次"没有任何闸）。这里按 token 记一个**进程内**分钟窗——从轻，因为这是
    内网信任面：不走 Redis；键用 token 的 sha256 前 16 位，不把凭据原文留在计数表里；
    未配 token 的本地拓扑按调用方 IP 归桶。多实例部署时各自计数（与幂等、event_hub
    的单进程前提一致）。`/hello`、`/health` 不带本依赖，探活不受影响。
    """
    expected = settings.agent_internal_token
    provided_text = (x_agent_token or "").strip()
    if expected and not hmac.compare_digest(provided_text.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid agent token")
    client_ip = request.client.host if request.client is not None else None
    _agent_rate_guard(provided_text, client_ip)


# ---------- agent 面速率桶（审查 P1-8） ----------
#
# X-Agent-Token 只做身份校验：token 一旦泄露，调用方可以无限打（单请求有
# RunLimits 封顶，但"打多少次"没有任何闸，墙钟与成本都无上界）。这里按 token
# 加一个**进程内**分钟窗——方案从轻，因为这是内网信任面：
#   - 不走 Redis（agent 面本来就是内网直调，一个进程内的列表足够拦脚本式滥用）；
#   - 键用 token 的 sha256 前 16 位，不把凭据原文留在计数表里；
#   - 未配 token 的本地/回环拓扑按调用方 IP 归桶（validate_boot 已保证"非回环 +
#     无 token"启动即拒，所以这条只覆盖本地/测试形态）。
# 多实例部署时各自计数（与幂等、event_hub 的单进程前提一致）。
_AGENT_WINDOW_SECONDS = 60
_AGENT_RATE_LOCK = threading.Lock()
_AGENT_RATE_BUCKETS: dict[str, list[float]] = {}


def _agent_bucket_key(x_agent_token: str | None, client_ip: str | None) -> str:
    material = (x_agent_token or "").strip()
    if material:
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"ip:{client_ip or 'unknown'}"


def _agent_rate_guard(x_agent_token: str | None, client_ip: str | None) -> None:
    """记一次并判窗；超限 429（退避节奏由调用方自定）。"""
    limit = settings.agent_rate_limit_per_minute
    if limit <= 0:
        return
    now = time.monotonic()
    key = _agent_bucket_key(x_agent_token, client_ip)
    with _AGENT_RATE_LOCK:
        hits = [stamp for stamp in _AGENT_RATE_BUCKETS.get(key, []) if now - stamp < _AGENT_WINDOW_SECONDS]
        hits.append(now)
        _AGENT_RATE_BUCKETS[key] = hits
        # 防表增长：桶数超阈值时顺手丢掉已过期的桶
        if len(_AGENT_RATE_BUCKETS) > 512:
            expired = [k for k, v in _AGENT_RATE_BUCKETS.items() if not v or now - v[-1] > _AGENT_WINDOW_SECONDS]
            for stale_key in expired:
                _AGENT_RATE_BUCKETS.pop(stale_key, None)
    if len(hits) > limit:
        raise HTTPException(status_code=429, detail="too many agent requests")


def reset_agent_rate_for_tests() -> None:
    """测试夹具用：清空进程内桶（跨用例不串味）。"""
    with _AGENT_RATE_LOCK:
        _AGENT_RATE_BUCKETS.clear()


@router.get("/hello")
def hello() -> ApiResponse[str]:
    return ApiResponse.ok("hello from travel-agent-python")


@router.get("/health")
def health() -> ApiResponse[dict]:
    return ApiResponse.ok({"status": "up"})


@router.get("/health/deep")
def health_deep() -> ApiResponse[dict]:
    """深度探活（审计 §3.2.6/§3.5.4 → P1-6）：MySQL SELECT 1 + Redis ping，结果缓存 ~10s。

    与浅探分工（报告拍板）：Dockerfile/compose 的 HEALTHCHECK 与部署等待继续打
    /health（容器编排只关心进程活着，不打扰依赖抖动）；这里供部署冒烟与外部拨测
    ——依赖挂掉时 503 + status=degraded（信封 data 带逐依赖状态），而不是把
    「依赖不可用」伪装成健康。LLM 不探（深探是可用性检查，不为探活花钱）。
    与 /health 同款匿名：边缘（Caddy/nginx）已把 /api/agent/** 对公网 403。
    """
    payload = deep_health.snapshot()
    ok = payload.get("status") == deep_health.UP
    body = ApiResponse.ok(payload) if ok else ApiResponse(code=503, message=deep_health.DEGRADED, data=payload)
    # 503 语义靠 status_code 表达，直返 JSONResponse（wire 与 ApiResponse 一致）
    return cast(
        ApiResponse[dict],
        JSONResponse(
            content=body.model_dump(mode="json", by_alias=True),
            status_code=200 if ok else 503,
        ),
    )


@router.get("/v1/metrics")
def agent_metrics(_auth: None = Depends(require_internal_token)) -> ApiResponse[dict]:
    """返回进程内聚合指标；生产环境应替换为 Prometheus/OTel exporter。"""
    return ApiResponse.ok(metrics.snapshot())


@router.get("/v1/usage")
def agent_usage(
    range: str = "24h", limit: int = 200, offset: int = 0, _auth: None = Depends(require_internal_token)
) -> ApiResponse[dict]:
    """SQLite 落库的 LLM 用量历史（汇总/场景/模型/趋势/明细），重启不清零。

    时间窗与分桶规则在 `usage_store.report` 里，与 `/api/admin/llm-usage` 同源。
    """
    return ApiResponse.ok(usage_store.report(range, limit, offset))


@router.get("/v1/metrics/prometheus", response_class=PlainTextResponse)
def agent_metrics_prometheus(_auth: None = Depends(require_internal_token)) -> PlainTextResponse:
    return PlainTextResponse(metrics.prometheus_text(), media_type="text/plain; version=0.0.4")


@router.get("/v1/tools")
def agent_tools(_auth: None = Depends(require_internal_token)) -> ApiResponse[list[dict]]:
    """返回已注册工具的脱敏治理元数据，不返回可执行 handler。"""
    return ApiResponse.ok(registry.public_specs())


@router.get("/v1/runs/{run_id}")
def agent_run_trace(run_id: str, _auth: None = Depends(require_internal_token)) -> ApiResponse[dict]:
    """查询最近一次运行的脱敏轨迹；轨迹过期后返回 404。"""
    trace = metrics.get_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="agent run not found or expired")
    return ApiResponse.ok(trace)


@router.post("/v1/generate")
@scene("generate")
def generate(
    req: GenerateRequest, x_request_id: str | None = Header(default=None), _auth: None = Depends(require_internal_token)
) -> ApiResponse[GenerateResponse]:
    trace = None
    try:
        with observe_run(request_id=x_request_id) as trace:
            result = run_generate(req)
        payload = ApiResponse.ok(result)
        # 为了带 X-Agent-Run-ID 头刻意直返 JSONResponse；wire 与 ApiResponse 完全一致
        return cast(
            ApiResponse[GenerateResponse],
            JSONResponse(
                content=payload.model_dump(mode="json", by_alias=True),
                headers={"X-Agent-Run-ID": trace.run_id, "X-Request-ID": trace.request_id},
            ),
        )
    except Exception as e:
        payload = _fail_payload(e, "generate")
        headers = {"X-Agent-Run-ID": trace.run_id, "X-Request-ID": trace.request_id} if trace is not None else None
        return cast(
            ApiResponse[GenerateResponse],
            JSONResponse(content=payload.model_dump(mode="json", by_alias=True), headers=headers),
        )


@router.post("/v1/adjust")
@scene("chat")
def adjust(req: AdjustRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[AdjustResponse]:
    return ApiResponse.ok(run_adjust(req))


@router.post("/v1/clarify")
@scene("clarify")
def clarify(req: ClarifyRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[ClarifyResponse]:
    try:
        return ApiResponse.ok(run_clarify(req))
    except ValueError as e:
        return ApiResponse.fail(str(e))


@router.post("/v1/edit-ops")
@scene("chat")
def edit_ops(req: EditOpRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[list[dict]]:
    try:
        ops = run_edit_ops(req)
        # 输出 snake_case 原始键，避免 WireModel 的 camel 别名影响跨语言消费方
        return ApiResponse.ok([op.model_dump() for op in ops])
    except ValueError as e:
        return ApiResponse.fail(str(e))


@router.post("/v1/plan-context")
@scene("assist")
def plan_context(req: PlanContextRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[dict]:
    # itinerary_id 透传给研究链路发布进度事件；旧调用方不传则不发布
    return ApiResponse.ok(run_plan_context(req.city, req.preferences, itinerary_id=req.itinerary_id))


@router.post("/v1/generate-day")
@scene("generate")
def generate_day(
    req: GenerateDayRequest,
    x_request_id: str | None = Header(default=None),
    _auth: None = Depends(require_internal_token),
) -> ApiResponse[DailyPlan]:
    trace = None
    try:
        with observe_run(request_id=req.request_id or x_request_id, action_id=req.action_id) as trace:
            result = run_generate_day(req)
        payload = ApiResponse.ok(result)
        return cast(
            ApiResponse[DailyPlan],
            JSONResponse(
                content=payload.model_dump(mode="json", by_alias=True),
                headers={"X-Agent-Run-ID": trace.run_id, "X-Request-ID": trace.request_id},
            ),
        )
    except Exception as e:
        payload = _fail_payload(e, "generate-day")
        headers = {"X-Agent-Run-ID": trace.run_id, "X-Request-ID": trace.request_id} if trace is not None else None
        return cast(
            ApiResponse[DailyPlan],
            JSONResponse(content=payload.model_dump(mode="json", by_alias=True), headers=headers),
        )


@router.post("/v1/generate-stream")
@scene("generate")
def generate_trip_stream(
    req: GenerateDayRequest,
    x_request_id: str | None = Header(default=None),
    _auth: None = Depends(require_internal_token),
) -> StreamingResponse:
    """整段流式生成：JSON Lines 逐行产出 start / day / day_patch / suggestions / done / error 事件。

    与 /v1/generate 的区别：LLM 边流边解析、逐天事件即时下发。落库与修复都在本进程
    业务编排侧（itinerary_generation 的整段流式腿）：边收事件边逐天落库，事件先过
    契约校验（schemas/stream_events，即 contracts/stream_events.schema.json 的源），
    流中断/缺天由既有逐日兜底循环补齐后再写终态；前端经业务侧 SSE 逐天点亮。

    线程模型：Starlette 会把同步生成器丢进线程池逐次 next()，contextvar
    的 token（trace/scene/limits）跨线程 reset 会抛 "created in a different
    Context" 并掐断响应。因此生成整体跑在单一 worker 线程（上下文完整），
    事件经有界队列转交异步响应生成器（见 _bridge_worker_events）；
    客户端断开即取消生成：在途 LLM 流被掐断，token 停止消耗。
    """

    def _producer(cancel: threading.Event) -> Iterator[dict]:
        try:
            with use_scene("generate"), observe_run(request_id=req.request_id or x_request_id) as trace:
                yield to_wire(StartEvent(type="start", run_id=trace.run_id))
                yield from run_generate_trip_stream(req, cancel=cancel)
        except Exception as e:
            logger.warning("generate-stream failed: %s", e)
            yield to_wire(ErrorEvent(type="error", message="生成服务暂不可用，请稍后重试"))

    return StreamingResponse(
        _bridge_worker_events(_producer),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/replan-local")
@scene("generate")
def replan_local(
    req: LocalReplanRequest,
    x_request_id: str | None = Header(default=None),
    _auth: None = Depends(require_internal_token),
) -> ApiResponse[dict]:
    trace = None
    try:
        with observe_run(request_id=req.request_id or x_request_id, action_id=req.action_id) as trace:
            result = run_local_replan(req)
        payload = ApiResponse.ok(result)
        return cast(
            ApiResponse[dict],
            JSONResponse(
                content=payload.model_dump(mode="json", by_alias=True),
                headers={"X-Agent-Run-ID": trace.run_id, "X-Request-ID": trace.request_id},
            ),
        )
    except Exception as exc:
        payload = _fail_payload(exc, "replan-local")
        headers = {"X-Agent-Run-ID": trace.run_id, "X-Request-ID": trace.request_id} if trace is not None else None
        return cast(
            ApiResponse[dict],
            JSONResponse(content=payload.model_dump(mode="json", by_alias=True), headers=headers),
        )


@router.post("/v1/chat-turn")
@scene("chat")
def chat_turn(
    req: ChatTurnRequest,
    confirmation_thread: str | None = None,
    _auth: None = Depends(require_internal_token),
) -> ApiResponse[ChatTurnResponse]:
    """对话回合（PR-6 确认流）：传 confirmation_thread 时，需确认的提案会在该 thread
    上 interrupt 暂停（提案落 checkpoint）；业务确认端经 resume_confirmation 以
    Command(resume=…) 续跑复核。不传则为旧直调语义（不进图）。
    """
    try:
        return ApiResponse.ok(run_chat_turn(req, confirmation_thread=confirmation_thread))
    except ValueError as e:
        return ApiResponse.fail(str(e))


@router.post("/v1/butler-note")
@scene("assist")
def butler_note(
    req: ButlerNoteRequest, _auth: None = Depends(require_internal_token)
) -> ApiResponse[ButlerNoteResponse]:
    try:
        # model_dump() 输出 snake_case 键，与 run_butler_note 读取的键一致
        return ApiResponse.ok(ButlerNoteResponse(note=run_butler_note(req.model_dump())))
    except Exception as exc:
        logger.warning("butler_note failed: %s", exc)
        return ApiResponse.ok(ButlerNoteResponse(note=""))


@router.post("/v1/poi-intros")
@scene("assist")
def poi_intros(req: PoiIntrosRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[PoiIntrosResponse]:
    try:
        # Pydantic 已保证 list[str]；沿用历史行为过滤空名
        names = [n for n in req.names if n]
        # M3-②：intent 透传进介绍 Prompt（为空时由 butler 层降级为口碑/地理理由）
        return ApiResponse.ok(PoiIntrosResponse(intros=run_poi_intros(req.city, names, intent=req.intent)))
    except Exception as exc:
        # 空结果是被测试钉住的降级契约（`test_poi_intros_degrades_to_empty_on_error`），
        # 不改；但"静默吞掉"不是契约的一部分——没有这行日志，故障与"确实没有介绍"
        # 在两侧都无从区分。
        logger.warning("poi_intros failed: %s", exc)
        return ApiResponse.ok(PoiIntrosResponse())


@router.post("/v1/poi-nearby")
@scene("assist")
def poi_nearby(req: PoiNearbyRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[PoiNearbyResponse]:
    """同城权威 POI 近邻（轻量 GraphRAG）：名称解析坐标或直接传坐标。

    坐标缺失时返回空列表——附近推荐只使用权威库真实坐标，不伪造。
    参数换算保持与改造前逐项一致：0 值 falsy 归默认/None；坐标经
    Pydantic lax 转换后原样透传（0.0 仍传 0.0，由底层判无效坐标）。
    """
    try:
        rows = find_nearby_pois(
            req.city,
            name=(req.name or None),
            latitude=req.latitude,
            longitude=req.longitude,
            limit=(req.limit or 5),
            radius_m=(req.radius_m or None),
            category=(req.category or None),
        )
        return ApiResponse.ok(PoiNearbyResponse(items=[PoiNearbyItem.model_validate(r) for r in rows]))
    except Exception as exc:
        logger.warning("poi_nearby failed: %s", exc)
        return ApiResponse.ok(PoiNearbyResponse())


@router.post("/v1/city-guide")
@scene("assist")
def city_guide(req: CityGuideRequest, _auth: None = Depends(require_internal_token)) -> ApiResponse[CityGuideResponse]:
    try:
        # by_alias=True：user_input 字段 dump 成 wire 键 "input"，与 run_city_guide 读取的键一致
        data = run_city_guide(req.model_dump(by_alias=True))
        return ApiResponse.ok(CityGuideResponse.model_validate(data))
    except Exception:
        return ApiResponse.fail("城市引导服务暂不可用")
