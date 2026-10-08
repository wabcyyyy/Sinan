"""僵尸生成任务的自动续跑（移植自 Java `ItineraryStatusRecovery`）。

Java 用 `@Scheduled(fixedDelay=60s, initialDelay=60s)` + `ApplicationReadyEvent` 各跑一次；
这里是**一个 daemon 线程 + 同样的节奏**，启动时在 lifespan 里先跑一次。判定口径保持逐字：

- 活跃阈值 `updated_at < now - 5min`。这条完全依赖 MySQL 的
  `ON UPDATE CURRENT_TIMESTAMP`——任何一次状态写（markRunning/markFailed/persist）都会刷新
  `itinerary_day.updated_at`，这就是"活跃天心跳"。Python 侧同样靠列默认值，不在代码里手写时间。
- 老数据没有 `gen_state`，所以两个分支都要带 `gen_state IS NULL AND status IN (1,3)` 的回落。
- 终态 PARTIAL（2026-10-07，P0-3）也进失败分支：只自动补「空天」（无可见内容的未成功天），
  有内容的天留给用户显式的一键重生成端点，防把正在编辑的行程卷进重生成。
- `gen_resumed=1` 表示"已经自动续跑过一次"，再失败不再重拉（防失败→重生成死循环）。
- 续跑去重锁 `gen:resume:{id}` TTL 10 分钟（大于一次生成的读超时 + 落库时间）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update

from app.agent import resume_day
from app.common import cron
from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.services import day_persistence, generation_gate, itinerary_generation, itinerary_query, llm_gateway_service

logger = logging.getLogger(__name__)

IDLE_SECONDS = 5 * 60
SCAN_INTERVAL_SECONDS = 60
# Java 在 ApplicationReadyEvent 上立刻扫一次；这里把这趟扫描挪进线程并延后几秒，
# 免得"启动应用"这件事依赖数据库可用（测试里 `with TestClient(app)` 会走 lifespan）。
STARTUP_DELAY_SECONDS = 5


def rebuild_request(main: ItineraryMain) -> itinerary_generation.GenerateCommand:
    """从行程主表反推生成参数（与 Java `rebuildRequest` 同口径）。

    恢复保真（P0-3）：origin_city / intent / requirements 一并读回——前两者
    直接重建命令，续跑的逐日生成与首次跑同一质量；缺了它们，续跑天会静默
    丢出发地（不查航班）与一句话意图（意图关键词研究全空）。
    """
    preferences = (
        []
        if not main.preferences or not main.preferences.strip()
        else [part for part in main.preferences.split(",") if part.strip()]
    )
    return itinerary_generation.GenerateCommand(
        city=main.city,
        days=main.days,
        persons=main.persons if main.persons is not None else 1,
        stay_nights=main.stay_nights if main.stay_nights is not None else max(main.days - 1, 0),
        start_date=main.start_date,
        end_date=main.end_date,
        budget=main.budget,
        preferences=preferences,
        hotel_tier=main.hotel_tier,
        region_hint=None,
        requirements=main.requirements,
        intent=main.intent,
        origin_city=main.origin_city,
    )


def recover() -> int:
    """扫描一轮，返回发生状态变更的行程数（便于测试与运维断言）。

    注意阈值口径：`updated_at` 由 MySQL 的 `ON UPDATE CURRENT_TIMESTAMP` 填，而这里用的是
    应用侧 `datetime.now()`。两者必须同一时区，否则这个 5 分钟窗口会整体平移（实例在 UTC、
    库在 +08:00 时，僵尸判定会提前 8 小时命中）。部署保持两侧同时区。
    """
    active_after = datetime.now() - timedelta(seconds=IDLE_SECONDS)
    stale = ItineraryMain.updated_at < active_after
    idle_generating = and_(
        stale,
        or_(
            ItineraryMain.gen_state == "GENERATING", and_(ItineraryMain.gen_state.is_(None), ItineraryMain.status == 1)
        ),
    )
    failed_resumable = and_(
        stale,
        # PARTIAL（2026-10-07，P0-3）：终态部分交付此前不在谓词内、零次自动恢复，
        # 半成品行程只能靠用户手动自救。纳入后与 FAILED 共用同一套封顶语义：
        # gen_resumed 一次性旗 + resume 锁 + 日锁（recover_one），补齐不会无限重试。
        or_(
            ItineraryMain.gen_state.in_(("FAILED", "PARTIAL")),
            and_(ItineraryMain.gen_state.is_(None), ItineraryMain.status == 3),
        ),
    )
    changed = 0
    with session_scope() as session:
        generating = session.execute(select(ItineraryMain).where(idle_generating)).scalars().all()
        failed = session.execute(select(ItineraryMain).where(failed_resumable)).scalars().all()
        targets = [(main.id, main.user_id, False) for main in generating] + [
            (main.id, main.user_id, True) for main in failed
        ]
    for itinerary_id, user_id, failed_resume in targets:
        try:
            if recover_one(itinerary_id, failed_resume, active_after):
                changed += 1
                itinerary_query.evict_detail(user_id, itinerary_id)
        except Exception as exc:
            logger.warning("could not resume itinerary %s: %s", itinerary_id, exc)
    return changed


def recover_one(itinerary_id: int, failed_resume: bool, active_after: datetime | None = None) -> bool:
    """返回是否真的动了状态。"""
    # 存活探测（P1-1）：本进程里这次生成还在跑/排队（研究段最坏静默 ≈19 分钟 ≫
    # 5 分钟阈值）时，updated_at 老 ≠ 僵尸——直接不碰，消灭"对活着的生成二次放行"
    # 的双跑窗口。单进程前提同 event_hub/幂等；多实例需升级 DB 租约。
    if itinerary_generation.is_planning_active(itinerary_id):
        return False
    cutoff = active_after or (datetime.now() - timedelta(seconds=IDLE_SECONDS))
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return False
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        day_ids_with_items = {
            row
            for row in session.execute(
                select(ItineraryItem.day_id).where(ItineraryItem.itinerary_id == itinerary_id).distinct()
            )
            .scalars()
            .all()
        }
        completed_days = sum(1 for day in days if _day_done(day, day_ids_with_items))
        if days and completed_days == len(days) and len(days) == main.days:
            # 数据其实齐了，只是终态没写上：补终态，不重跑
            _mark_completed(session, itinerary_id, main.city, main.days)
            logger.info("recovered completed itinerary %s", itinerary_id)
            return True

        if main.gen_state == "PARTIAL":
            # 交接陷阱护栏（P0-3）：PARTIAL 的自动补齐只碰「空天」（无可见内容的未成功天）。
            # 有可见内容的天可能是终检重置的草稿，也可能是用户手动补的安排——DB 里两者
            # 无法区分，一律不自动碰，留给用户显式点「重新生成这一天」（意图明确、且带
            # 配额豁免语义）；否则恢复轮会把正在正常编辑的行程卷进重生成、软删用户内容。
            days_with_live_items = {
                row
                for row in session.execute(
                    select(ItineraryItem.day_id)
                    .where(ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.deleted == 0)
                    .distinct()
                )
                .scalars()
                .all()
            }
            if any(day.generation_status != "SUCCEEDED" and day.id in days_with_live_items for day in days):
                return False

        if any(day.generation_status == "RUNNING" and day.updated_at and day.updated_at > cutoff for day in days):
            return False
        if failed_resume and not _resumable_failed_trip(days):
            return False
        if failed_resume and main.gen_resumed:
            # 防死循环护栏（docstring 与 fail_trip 早已承诺此语义，V1 起一直无读取方）：
            # gen_resumed=1 = 已自动续跑过一次且再次失败，重拉只会重演同一失败。
            # resume 锁 TTL 只有 10 分钟，过期后挡住重拉的就只剩这面旗。
            # PARTIAL 纳入扫描（P0-3）后同受此闸封顶：自动补齐只有一次机会，再失败
            # 留给用户走「重新生成这一天」端点显式自救——前提是 complete_trip 在写
            # PARTIAL 终态时不清这面旗（否则每轮扫描都重拉，白烧 LLM 成无限补齐）。
            return False
        if not generation_gate.try_resume_lock(itinerary_id):
            return False
        if failed_resume:
            session.execute(
                update(ItineraryMain)
                .where(ItineraryMain.id == itinerary_id)
                .values(gen_resumed=True, gen_state="GENERATING", status=1)
            )
        command = rebuild_request(main)
        user_id = main.user_id
        pending = [day.day_no for day in days if not _day_done(day, day_ids_with_items)]

    # PR-3 从 checkpoint 续跑（锁内、会话外：续跑要跑图，不抱着 DB 会话跑 LLM）：
    # 断在图里的天直接续完落库（已完成超步不重跑、已产出天不重生成），并把研究上下文
    # 从检查点找回喂给兜底排程——"僵尸整段续跑"（整段研究 + 整段重生成）由此降级为
    # 无检查点时的兜底，长行程流断后的恢复不再丢天、也不整段重来。
    context: dict | None = None
    remaining: list[int] = []
    resumed_any = False
    fingerprint = generation_gate.request_fingerprint(command)
    # BYOK 路由（决策 5 的依据腿）：resume_day 在恢复线程内直接跑 day_stream →
    # get_llm_client，不包 route_scope 会静默回落默认通道——续跑天与已生成天出自
    # 不同模型家族且烧运营方 key。下方 submit_planning 腿由 plan_days 内部自覆盖。
    # 密文解不开（ApiError 409）必须就地终态化并放 resume 锁：扫描周期重拉也必败，
    # 漏放锁则这条行程永远进不了恢复（终审补丁）。
    try:
        with llm_gateway_service.route_scope(user_id):
            for day_no in pending:
                action_id = f"day-{itinerary_id}-{day_no}"
                resumed = resume_day(action_id)
                if resumed is not None and resumed.context is not None:
                    context = context or resumed.context
                if resumed is None or resumed.plan is None:
                    remaining.append(day_no)
                    continue
                day_persistence.persist(itinerary_id, command, day_no, resumed.plan, action_id, fingerprint)
                logger.info("resumed itinerary %s day %s from checkpoint", itinerary_id, day_no)
                resumed_any = True
    except ApiError as exc:
        generation_gate.release_resume_lock(itinerary_id)
        day_persistence.fail_trip(itinerary_id, str(exc) or None, user_facing=True)
        logger.warning("byok route broken, failed itinerary %s: %s", itinerary_id, exc)
        return True

    if pending and not remaining:
        # 全部从检查点续完：补终态（与"数据齐了"分支同口径），不整段重排
        with session_scope() as session:
            _mark_completed(session, itinerary_id, command.city, command.days)
        generation_gate.release_resume_lock(itinerary_id)
        return True

    try:
        itinerary_generation.submit_planning(user_id, itinerary_id, command, context=context)
    except itinerary_generation.TaskRejected as rejected:
        generation_gate.release_resume_lock(itinerary_id)
        logger.warning("could not resume itinerary %s: %s", itinerary_id, rejected)
        return False
    # failedResume 分支返回 True（这条行程确实被重拉了）；僵尸分支保持 Java 的返回值
    # ——除非本轮真从检查点续出了天，那确实动了状态。
    return failed_resume or resumed_any


def _resumable_failed_trip(days: Sequence[ItineraryDay]) -> bool:
    """可续跑的失败行程：至少还有一天没成功（有活可干就值得续跑一次）。

    R2-F7（2026-10-05 拍板：恢复侧收窄）：旧口径要求"每一天都 FAILED 且失败原因
    非空"，部分成功（day1 成 day2 败/PENDING）的行程两分支都不命中、永不恢复，
    用户唯一出路是整趟重建（夜审 trip 89/90/91 全是此形状）。放宽后允许增量补天：
    SUCCEEDED 天在 plan_days 逐日循环里幂等跳过、不覆盖既有内容；重复失败的代价
    由 gen_resumed 读取闸封顶（recover_one，恢复一次后不再重拉）。
    """
    return bool(days) and any(day.generation_status != "SUCCEEDED" for day in days)


def _day_done(day: ItineraryDay, day_ids_with_items: set[int]) -> bool:
    """ "这一天已经有产出"的判定（补终态与续跑挑选共用一处口径）。"""
    return day.generation_status == "SUCCEEDED" or (day.generation_status is None and day.id in day_ids_with_items)


def _mark_completed(session, itinerary_id: int, city: str, days: int) -> None:
    """补终态（数据齐了 / 检查点续完两个分支共用一处口径）。"""
    session.execute(
        update(ItineraryMain)
        .where(ItineraryMain.id == itinerary_id)
        .values(status=2, gen_state="COMPLETED", gen_finished_at=datetime.now(), title=f"{city}{days}日游")
    )


# 周期调度统一走 app.common.cron（G-3.3）：本模块只负责"扫什么"，不负责
# "多久扫一次/怎么停"。pytest 环境由 cron 自动 no-op（不再自己判环境）。
CRON_NAME = "generation-recovery"


def register_loop() -> None:
    """把续跑扫描登记进周期任务表（幂等；main.py lifespan 调用）。"""
    cron.register(
        CRON_NAME,
        SCAN_INTERVAL_SECONDS,
        recover,
        startup_delay_seconds=STARTUP_DELAY_SECONDS,
    )
