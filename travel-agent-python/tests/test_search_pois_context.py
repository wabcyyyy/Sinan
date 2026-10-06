"""R3-F1（2026-10-05）：search_pois 的内层线程池必须继承 contextvars。

catalog._search_pois 裸 `pool.submit(tools.xxx, …)` 时，worker 里
`current_limits()` 是 None（检索预算"放行不记账"）、trace recorder 是 None
（事件静默丢）。提交点逐 submit `copy_context().run` 后，worker 与发起线程
共享同一份上下文快照（姿势同 research/supervisor 的注释约定）。
"""

from __future__ import annotations

import app.agent  # noqa: F401  先行完成 agent 门面导入（同 test_llm_route.py 口径）
from app.agent.runtime.run_limits import begin_limits, current_limits, end_limits
from app.agent.tools.registry.catalog import _search_pois


def test_search_pois_workers_inherit_limits_context(monkeypatch) -> None:
    from app.agent.tools import impl as tools

    seen_in_worker: list[object] = []
    monkeypatch.setattr(tools, "search_attractions", lambda *_a, **_k: seen_in_worker.append(current_limits()) or [])
    monkeypatch.setattr(tools, "search_foods", lambda *_a, **_k: [])
    monkeypatch.setattr(tools, "get_consumption", lambda *_a, **_k: {})

    token = begin_limits()
    try:
        limits = current_limits()
        result = _search_pois(city="杭州")
    finally:
        end_limits(token)

    assert result == {"attractions": [], "foods": [], "consumption": {}}
    assert len(seen_in_worker) == 1, "三个检索工具有一个必须真的在 worker 里跑过"
    assert seen_in_worker[0] is limits, "worker 内必须看到发起线程的同一份预算对象（非 None、非新建）"
