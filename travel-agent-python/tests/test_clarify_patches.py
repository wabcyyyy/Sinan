"""M1a clarify patch 链路测试：state 权威 + patches 确定性应用（LLM 全 mock，不打真实模型）。

mock 手法对齐 tests/test_clarify_intake.py：monkeypatch clarify.get_llm_client 返回固定输出。
"""

from app.agent.editing import clarify as clarify_mod
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState


class _FakeClient:
    """签名对齐 llm_client.complete 的最小桩。"""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    def complete(self, prompt: str, system_prompt: str = "", temperature: float = 0) -> str:
        return self._raw


def _run(monkeypatch, raw: str, req: ClarifyRequest | None = None):
    monkeypatch.setattr(clarify_mod, "get_llm_client", lambda: _FakeClient(raw))
    return clarify_mod.run_clarify(req or ClarifyRequest(message="想去玩"))


def test_state_days_eight_survives_round_and_stays_blocked(monkeypatch) -> None:
    """state 是权威：8 天的累计状态过一轮后原值仍在（不被截 7、不被丢），协商态 blocked 且投影含 8。"""
    res = _run(
        monkeypatch,
        '{"question":null,"options":null}',
        ClarifyRequest(message="就按 8 天来", state=IntakeState(days=8)),
    )
    assert res.state.days == 8, "多轮协商不丢原值：响应 state 仍为 8"
    assert res.blocked is True
    assert res.ready is False
    assert res.slots["days"] == 8, "slots 兼容投影同步携带 8，老前端不回退"
    assert res.question is not None and "7 天" in res.question


def test_museum_category_patch_lands_in_requirements(monkeypatch) -> None:
    """LLM 输出 patches：add excluded_category museum 落进结构化需求，而不是自然语言残留。"""
    res = _run(
        monkeypatch,
        '{"patches":[{"op":"add","target":"excluded_category","name":"museum"}],"question":null,"options":null}',
    )
    assert res.state.requirements.excluded_categories == ["museum"]
    assert res.state.requirements.unresolved_requests == [], "词表内类别不算未决请求"


def test_required_place_day_change_across_turns_keeps_single_entry(monkeypatch) -> None:
    """「改成第二天去灵隐寺」跨轮：先 add day_no=1 再 set day_no=2，终态单条且 day_no=2（state 权威贯穿）。"""
    first = _run(
        monkeypatch,
        '{"patches":[{"op":"add","target":"required_place","name":"灵隐寺","day_no":1}],"question":null,"options":null}',
        ClarifyRequest(message="必去灵隐寺", state=IntakeState(city="杭州", days=3)),
    )
    assert [(p.name, p.day_no) for p in first.state.requirements.required_places] == [("灵隐寺", 1)]

    second = _run(
        monkeypatch,
        '{"patches":[{"op":"set","target":"required_place","name":"灵隐寺","day_no":2}],"question":null,"options":null}',
        ClarifyRequest(message="改成第二天去灵隐寺", state=first.state),
    )
    places = second.state.requirements.required_places
    assert len(places) == 1, "set 替换语义：不留 day_no=1 的冲突副本"
    assert places[0].name == "灵隐寺"
    assert places[0].day_no == 2
    assert second.state.days == 3, "跨轮基础槽位不丢"


def test_unsupported_category_patch_goes_to_unresolved(monkeypatch) -> None:
    """词表外类别被 apply 层拒绝：原话进 unresolved_requests 留底，绝不进 excluded_categories。"""
    res = _run(
        monkeypatch,
        '{"patches":[{"op":"add","target":"excluded_category","name":"bar"}],"question":null,"options":null}',
    )
    assert res.state.requirements.excluded_categories == []
    assert res.state.requirements.unresolved_requests == [
        "add excluded_category bar：暂不支持排除类别，当前支持：museum"
    ]


def test_legacy_slots_budget_marker_prevents_rewarning(monkeypatch) -> None:
    """老前端兼容：不带 state、slots 带 _budget_warned=True → 协商闸不重复触发，标记投影回传。"""
    slots = {"city": "成都", "days": 3, "persons": 2, "budget": 500, "_budget_warned": True}
    res = _run(
        monkeypatch,
        '{"question":null,"options":null}',
        ClarifyRequest(message="就按这个预算试试", slots=slots),
    )
    assert res.ready is True
    assert res.blocked is False, "negotiations 已标记：预算闸只拦一次，不二次打断"
    assert res.state.negotiations.get("_budget_warned") is True, "标记随 slots 播种进 state.negotiations"
    assert res.slots["_budget_warned"] is True, "响应投影回传标记，老前端下轮继续带回"


def test_response_state_present_and_slots_match_base_slots(monkeypatch) -> None:
    """响应契约：state 恒存在，slots 与 state 的基础槽位一致（兼容投影不漂移）。"""
    res = _run(
        monkeypatch,
        '{"city":"成都","origin_city":"北京","days":3,"persons":2,"budget":3000,"question":null,"options":null}',
    )
    assert isinstance(res.state, IntakeState)
    assert res.state.city == "成都"
    assert res.state.days == 3
    assert res.state.persons == 2
    assert res.slots["city"] == res.state.city
    assert res.slots["days"] == res.state.days
    assert res.slots["persons"] == res.state.persons
    assert res.slots["origin_city"] == res.state.origin_city
    assert res.slots["budget"] == res.state.budget
    assert res.ready is True


def test_invalid_patch_shape_skipped_others_still_applied(monkeypatch) -> None:
    """单条 patch 形状非法（target 不在枚举）只跳过该条：其余 patch 照常应用，且不进 unresolved。"""
    res = _run(
        monkeypatch,
        '{"patches":['
        '{"op":"set","target":"anything","value":1},'
        '{"op":"add","target":"excluded_category","name":"museum"}],'
        '"question":null,"options":null}',
    )
    assert res.state.requirements.excluded_categories == ["museum"], "合法 patch 不被坏邻居拖累"
    assert res.state.requirements.unresolved_requests == [], "抽取层跳过 ≠ 语义拒绝，不写 unresolved"
