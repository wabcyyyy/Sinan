"""生成 Prompt 文本指纹（审查 P2-6 / R8-Q2：消灭"改串不 bump"的荣誉制）。

问题：`eval_gate.py` 的期望版本常量（`EXPECTED_OPEN_DAY_PROMPT_VERSION` 等）是**手抄**
的——改 Prompt 正文却不 bump 版本号时，没有任何门禁会红。白天改了措辞、晚上 nightly
照跑，报告与基线的"同版本"就成了一句空话。

做法：把两套开放生成 Prompt 的**正文**（固定参数下的 canonical 输出，含逐日/整段两段
与酒店、节奏条款）取 sha256 落进报告；门禁侧用**同一个函数**在评测时刻重算源码现值，
两边不一致即红——作者必须显式改版本号（有意识认账）或改回措辞。

为什么哈希 canonical 输出而不是源文件字节：源文件里还有大量非 Prompt 代码（解析、
工具函数），按文件哈希会让任何重构都触发漂移告警，噪声会淹没真信号。canonical 参数
（day_no=1 / days=1 / needs_hotel=True）只覆盖正文变化面。

单一真源：本函数是**唯一实现**，llm_eval.py（写报告）与 eval_gate.py（查验）都 import 它；
两侧各写一份等于没门禁。
"""

from __future__ import annotations

import hashlib

from app.agent.generation.rules.day_policy import MAX_DAILY_ATTRACTIONS, MAX_DAILY_MINUTES, MIN_ACTIVE_MINUTES
from app.agent.generation.rules.generation_core import day_hotel_clause, hotel_prompt_clause
from app.prompts.open_generation import open_day_system_prompt, open_trip_system_prompt

_CANONICAL_SEPARATOR = "\x00"


class _CanonicalMemory:
    """WorkingMemory 的最小替身：只提供 Prompt 拼接真正读取的方法。"""

    def as_sorted_list(self) -> list[str]:
        return []


def prompt_text_sha256() -> str:
    """两套开放生成 Prompt 正文的 sha256（固定参数，不含动态数据块）。

    终检窗口参数取自 reflect 的常量：改窗口即改正文，指纹随之变化——这正是
    想要的效果（改判据必须显式 bump 版本并同步 eval_gate 的期望常量）。
    """
    parts = (
        open_day_system_prompt(
            day_no=1,
            pace="",
            hotel_clause=day_hotel_clause(True),
            hotel_hint="",
            mem=_CanonicalMemory(),
            min_active_minutes=MIN_ACTIVE_MINUTES,
            max_daily_minutes=MAX_DAILY_MINUTES,
            max_daily_attractions=MAX_DAILY_ATTRACTIONS,
        ),
        open_day_system_prompt(
            day_no=2,
            pace="",
            hotel_clause=day_hotel_clause(False),
            hotel_hint="",
            mem=_CanonicalMemory(),
            min_active_minutes=MIN_ACTIVE_MINUTES,
            max_daily_minutes=MAX_DAILY_MINUTES,
            max_daily_attractions=MAX_DAILY_ATTRACTIONS,
        ),
        open_trip_system_prompt(
            days=1,
            hotel_clause=hotel_prompt_clause(True, 1),
            min_active_minutes=MIN_ACTIVE_MINUTES,
            max_daily_minutes=MAX_DAILY_MINUTES,
        ),
        open_trip_system_prompt(
            days=1,
            hotel_clause=hotel_prompt_clause(False, 1),
            min_active_minutes=MIN_ACTIVE_MINUTES,
            max_daily_minutes=MAX_DAILY_MINUTES,
        ),
    )
    return hashlib.sha256(_CANONICAL_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
