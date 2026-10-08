"""生成幂等门（移植自 Java `ItineraryGenerationGate`）。

三层判重原语收拢在这里，编排层与落库层共用同一实现——Java 此前在两处各写一份
`verifyAction`/指纹，语义已经漂过一次才收敛成本类，迁移时不再把它拆回去。

指纹必须与 Java **逐字节一致**：双跑期一条行程可能由 Java 建壳、由 Python 续跑（反之亦然），
`generation_fingerprint` 不同就会 409「同一日期生成参数已发生变化」，用户看到的是
"点个刷新就生成不了"。因此 `String.valueOf(...)` 的取值形状（null → "null"、
BigDecimal → 带标度的 "2000.00"、LocalDate → ISO）都在 `_java_string` 里显式复刻。
（Java 退役后跨实现的逐字节一致不再是约束；budget 的标度归一是唯一的形状变更，
见 `request_fingerprint`——它修复的是"首次生成 vs 恢复重建"两侧的内部一致。）
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.common.envelope import ApiError
from app.db.models import ItineraryDay
from app.schemas.trip_requirements import TripRequirements, canonical_requirements_payload
from app.services import state_and_sessions

logger = logging.getLogger(__name__)

DAY_LOCK_TTL_SECONDS = 5 * 60  # 大于单日最坏生成耗时
RESUME_LOCK_TTL_SECONDS = 10 * 60  # 大于 Python 读超时 180s + 落库
RESUME_KEY_PREFIX = "gen:resume:"


def day_lock_key(itinerary_id: int, day_no: int) -> str:
    """日锁粒度：(itineraryId, dayNo)，避免两个任务并发生成同一天。"""
    return f"gen:day:{int(itinerary_id)}:{int(day_no)}"


def try_day_lock(itinerary_id: int, day_no: int) -> bool:
    return state_and_sessions.try_mark(day_lock_key(itinerary_id, day_no), DAY_LOCK_TTL_SECONDS)


def release_day_lock(itinerary_id: int, day_no: int) -> None:
    state_and_sessions.unmark(day_lock_key(itinerary_id, day_no))


def try_resume_lock(itinerary_id: int) -> bool:
    return state_and_sessions.try_mark(RESUME_KEY_PREFIX + str(int(itinerary_id)), RESUME_LOCK_TTL_SECONDS)


def release_resume_lock(itinerary_id: int) -> None:
    state_and_sessions.unmark(RESUME_KEY_PREFIX + str(int(itinerary_id)))


def verify_action(day: ItineraryDay, action_id: str, fingerprint: str) -> None:
    """该天是否可被本动作写入：不匹配即 409，防止旧任务覆盖新任务的生成结果。"""
    if day.generation_action_id is not None and action_id != day.generation_action_id:
        raise ApiError(409, "该日期已有不同的生成动作，请使用最新任务")
    if day.generation_fingerprint is not None and fingerprint != day.generation_fingerprint:
        raise ApiError(409, "同一日期生成参数已发生变化，请重新创建行程")


def request_fingerprint(request: Any) -> str:
    """生成参数指纹：同参数重试/续跑指纹一致可幂等续写，参数变了则拒绝覆写旧天。

    `request` 可以是 `GenerateCommand`/`GenerateRequest` 模型或等值 dict
    （恢复任务从库里重建时也是这个形状）。

    budget 标度归一（恢复保真）：首次生成侧是请求 JSON 反序列化的 Decimal
    （"2000"），恢复重建侧是 DECIMAL(12,2) 列回读的 Decimal（"2000.00"）——
    不归一的话带预算的行程一进恢复就被幂等门 409 拒掉。按列标度 quantize
    （ROUND_HALF_UP 对齐 MySQL 的入列舍入），两侧同过此函数，形状必然一致。

    指纹口径（M1a 双版本）：
    - v1（迁移前口径，9 段）：city/days/persons/stay_nights/budget/start_date/
      end_date/hotel_tier/preferences。**旧行程恢复专用**：`legacy_fingerprint=True`
      （rebuild 依据 requirements_json IS NULL 判定）时按 v1 计算，与存量指纹
      逐字节一致——口径升级不能把存量行程的恢复/重生成打成 409；
    - v2（新行程口径 = 完整输入身份）：v1 基础段 + intent + requirements 原文 +
      origin_city + 规范化结构化需求（canonical JSON）。带 "v2|" 版本前缀进
      hash，与 v1 值域天然不相交。改变到达窗口/必去指定日/出发地/有效意图
      都会改变指纹；全默认需求与 None 在 canonical（exclude_defaults）下等价。
    """

    def field(*names: str) -> Any:
        for name in names:
            value = request.get(name) if isinstance(request, dict) else getattr(request, name, None)
            if value is not None:
                return value
        return None

    budget = field("budget")
    if isinstance(budget, Decimal):
        budget = budget.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    preferences = field("preferences") or []
    parts = (
        _java_string(field("city"), empty_is_null=True),
        _java_string(field("days")),
        _java_string(field("persons")),
        _java_string(field("stay_nights", "stayNights")),
        _java_string(budget),
        _java_string(field("start_date", "startDate")),
        _java_string(field("end_date", "endDate")),
        _java_string(field("hotel_tier", "hotelTier"), empty_is_null=True),
        ",".join(str(preference) for preference in preferences),
    )
    if field("legacy_fingerprint"):
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    struct = field("requirements_struct", "requirementsStruct")
    if isinstance(struct, dict):
        struct = TripRequirements.model_validate(struct)
    canonical = json.dumps(
        canonical_requirements_payload(struct if isinstance(struct, TripRequirements) else None),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    v2_parts = (
        *parts,
        _java_string(field("intent"), empty_is_null=True),
        _java_string(field("requirements"), empty_is_null=True),
        _java_string(field("origin_city"), empty_is_null=True),
        canonical,
    )
    return hashlib.sha256(("v2|" + "|".join(v2_parts)).encode("utf-8")).hexdigest()


def _java_string(value: Any, *, empty_is_null: bool = False) -> str:
    """复刻 Java `String.valueOf(obj)`：null → "null"，数字/日期按原样标度输出。"""
    if value is None:
        return "" if empty_is_null else "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
