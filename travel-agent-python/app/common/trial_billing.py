"""赠送试用额度：人民币微元整数记账，逐次调用冻结、按配置单价结算。

这里的费用是应用按运营者配置的价目计算的试用消耗，不是供应商发票。
未知用量保留冻结额，须人工对账；重启不会释放。BYOK 不扣平台赠额。
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import SysUser, TrialLedger, TrialWallet
from app.db.session import session_scope

MICROS = 1_000_000
_payer: ContextVar[int | None] = ContextVar("trial_payer", default=None)


@contextmanager
def payer_scope(user_id: int) -> Iterator[None]:
    token = _payer.set(user_id)
    try:
        yield
    finally:
        _payer.reset(token)


def micros(cny: float | str | Decimal) -> int:
    amount = Decimal(str(cny))
    if not amount.is_finite() or amount < 0:
        raise ApiError(400, "金额须为非负有限数值")
    return int((amount * MICROS).to_integral_value(rounding=ROUND_CEILING))


def open_wallet(session: Session, user_id: int) -> None:
    amount = micros(settings.trial_initial_credit_cny)
    session.add(TrialWallet(user_id=user_id, balance_micros=amount))
    session.add(
        TrialLedger(
            id=str(uuid.uuid4()),
            user_id=user_id,
            kind="grant",
            status="settled",
            amount_micros=amount,
            detail={"operator": "registration", "reason": "一次性试用赠额"},
        )
    )


def grant(user_id: int, amount: float, reason: str, operator: str, request_id: str) -> None:
    value = micros(amount)
    if not 0 < value <= 100 * MICROS:
        raise ApiError(400, "单次赠额须大于 0 且不超过 100 元")
    with session_scope() as session:
        user = session.execute(select(SysUser).where(SysUser.id == user_id).with_for_update()).scalar_one_or_none()
        if user is None:
            raise ApiError(404, "用户不存在")
        row = session.get(TrialLedger, request_id)
        if row is not None:
            if row.user_id != user_id or row.kind != "grant" or row.amount_micros != value:
                raise ApiError(409, "同一赠额请求参数不一致")
            return
        wallet = session.execute(
            select(TrialWallet).where(TrialWallet.user_id == user_id).with_for_update()
        ).scalar_one_or_none()
        if wallet is None:
            wallet = TrialWallet(user_id=user_id, balance_micros=0, spent_micros=0)
            session.add(wallet)
        wallet.balance_micros += value
        session.add(
            TrialLedger(
                id=request_id,
                user_id=user_id,
                kind="grant",
                status="settled",
                amount_micros=value,
                detail={"operator": operator, "reason": reason},
            )
        )


def wallet_view(user_id: int) -> dict:
    with session_scope() as session:
        wallet = session.get(TrialWallet, user_id)
        entries = (
            session.execute(
                select(TrialLedger)
                .where(TrialLedger.user_id == user_id)
                .order_by(TrialLedger.created_at.desc())
                .limit(100)
            )
            .scalars()
            .all()
        )
        return {
            "userId": user_id,
            "balanceCny": (wallet.balance_micros if wallet else 0) / MICROS,
            "spentCny": (wallet.spent_micros if wallet else 0) / MICROS,
            "entries": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "status": row.status,
                    "amountCny": row.amount_micros / MICROS,
                    "reservedCny": row.reserved_micros / MICROS,
                    "model": row.model,
                    "detail": row.detail,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in entries
            ],
        }


def _price(base_url: str, model: str) -> dict[str, Decimal]:
    try:
        book = json.loads(settings.llm_price_cny_json)
        raw = book[base_url.rstrip("/") + "|" + model]
        rates = {key: Decimal(str(raw[key])) for key in ("input", "cached_input", "output")}
        if any(not value.is_finite() or value < 0 for value in rates.values()):
            raise ValueError("invalid price")
        return rates
    except (ValueError, TypeError, KeyError) as exc:
        raise ApiError(503, "当前模型尚未配置有效人民币单价，请联系管理员") from exc


def _cost(rates: dict[str, Decimal], prompt: int, cached: int, completion: int) -> int:
    # 单价单位：元/百万 token；结果直接为微元，避免浮点累计误差。
    value = rates["input"] * (prompt - cached) + rates["cached_input"] * cached + rates["output"] * completion
    return int(value.to_integral_value(rounding=ROUND_CEILING))


@dataclass
class Charge:
    id: str
    rates: dict[str, Decimal]

    def settle(self, usage: dict | None) -> None:
        if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
            return  # 无 usage 不能伪装免费；上下文退出时标 unknown。
        prompt, output = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        cached = int(
            usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        )
        if min(prompt, output, cached) < 0 or cached > prompt:
            raise ApiError(502, "模型返回的计费用量无效，额度暂被冻结")
        _settle(self.id, _cost(self.rates, prompt, cached, output), {"usage": usage})


def _settle(charge_id: str, amount: int, detail: dict) -> None:
    with session_scope() as session:
        row = session.execute(select(TrialLedger).where(TrialLedger.id == charge_id).with_for_update()).scalar_one()
        if row.status == "settled":
            return
        wallet = session.execute(
            select(TrialWallet).where(TrialWallet.user_id == row.user_id).with_for_update()
        ).scalar_one()
        wallet.balance_micros += row.reserved_micros - amount
        wallet.spent_micros += amount
        row.amount_micros, row.status = amount, "settled"
        row.detail = {**(row.detail or {}), **detail}


def reconcile(charge_id: str, amount: float, reason: str, operator: str) -> None:
    with session_scope() as session:
        row = session.get(TrialLedger, charge_id)
        if row is None or row.kind != "call":
            raise ApiError(404, "费用记录不存在")
        if row.status not in ("unknown", "settled"):
            raise ApiError(409, "调用仍在执行，暂不能对账")
    _settle(charge_id, micros(amount), {"reconciledBy": operator, "reason": reason})


def recover_reservations() -> None:
    """单实例启动：上个进程未结算的调用保留冻结，进入人工对账，绝不自动退款。"""
    with session_scope() as session:
        session.execute(update(TrialLedger).where(TrialLedger.status == "reserved").values(status="unknown"))


def _reserve(base_url: str, payload: dict, user_funded: bool) -> Charge | None:
    if not settings.trial_billing_enabled or user_funded:
        return None
    user_id = _payer.get()
    if user_id is None:
        raise ApiError(503, "付费调用缺少用户归属，已阻止请求")
    if payload.get("enable_search"):
        raise ApiError(503, "试用额度暂不支持供应商另行收费的联网插件")
    rates = _price(base_url, payload["model"])
    # UTF-8 字节数 + 协议余量保守预留文本输入；实际按 usage 结算。
    prompt_bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
    held = _cost({**rates, "cached_input": rates["input"]}, prompt_bound, 0, int(payload["max_tokens"]))
    charge_id = str(uuid.uuid4())
    with session_scope() as session:
        changed = session.execute(
            update(TrialWallet)
            .where(TrialWallet.user_id == user_id, TrialWallet.balance_micros >= held)
            .values(balance_micros=TrialWallet.balance_micros - held)
        )
        if cast(CursorResult, changed).rowcount != 1:
            raise ApiError(402, "试用余额不足或有待核实费用，请在设置页查看余额")
        session.add(
            TrialLedger(
                id=charge_id,
                user_id=user_id,
                kind="call",
                status="reserved",
                reserved_micros=held,
                model=payload["model"],
                detail={"baseUrl": base_url, "pricesCnyPerMillion": {k: str(v) for k, v in rates.items()}},
            )
        )
    return Charge(charge_id, rates)


@contextmanager
def billable_call(base_url: str, payload: dict, *, user_funded: bool = False) -> Iterator[Charge | None]:
    charge = _reserve(base_url, payload, user_funded)
    try:
        yield charge
    finally:
        if charge is not None:
            with session_scope() as session:
                session.execute(
                    update(TrialLedger)
                    .where(TrialLedger.id == charge.id, TrialLedger.status == "reserved")
                    .values(status="unknown")
                )
