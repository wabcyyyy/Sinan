"""CH1 intake 升级：clarify 多轮收集器的分支行为（LLM 全 mock，不打真实模型）。"""

from app.agent.editing import clarify as clarify_mod
from app.schemas.trip import MAX_TRIP_DAYS, ClarifyRequest, ClarifyResponse


class _FakeClient:
    """签名对齐 llm_client.complete 的最小桩。"""

    def __init__(self, raw: str | Exception) -> None:
        self._raw = raw

    def complete(self, prompt: str, system_prompt: str = "", temperature: float = 0) -> str:
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _run(monkeypatch, raw: str | Exception, slots: dict | None = None) -> ClarifyResponse:
    monkeypatch.setattr(clarify_mod, "get_llm_client", lambda: _FakeClient(raw))
    return clarify_mod.run_clarify(ClarifyRequest(message="想去玩", slots=slots or {}))


def test_merges_slots_and_reports_ready(monkeypatch) -> None:
    raw = '{"city":"成都","origin_city":"北京","days":3,"persons":2,"budget":3000,"question":null,"options":null}'
    res = _run(monkeypatch, raw, slots={"city": "成都"})
    assert res.ready is True
    assert res.blocked is False
    assert res.degraded is False, "正常抽取轮不得带降级标记"
    assert res.question is None
    assert res.options == []
    assert res.slots["origin_city"] == "北京"
    assert res.slots["days"] == 3


def test_question_and_options_align_with_first_missing_slot(monkeypatch) -> None:
    """F10①：LLM 自由发挥的追问/选项不再透传——问哪个槽位就给哪个槽位的候选。"""
    raw = '{"question":"从哪个城市出发？","options":["3 天","5 天","7 天"]}'
    res = _run(monkeypatch, raw)
    assert res.ready is False
    assert res.missing == ["city", "days", "persons"]
    assert res.question == "还想确认一下目的地城市～"
    assert res.options == ["帮我推荐目的地"]


def test_optional_slot_never_shadows_required(monkeypatch) -> None:
    """F10②：必填未齐时可选槽位（出发日期）不可能成为追问对象，人数必填优先。"""
    raw = (
        '{"city":"成都","days":3,"start_date":"2026-10-01",'
        '"question":"出发日期定在哪天？","options":["10月1日","10月2日","还没定"]}'
    )
    res = _run(monkeypatch, raw)
    assert res.slots["start_date"] == "2026-10-01", "可选槽位照常抽取入库"
    assert res.missing == ["persons"]
    assert res.question == "还想确认一下出行人数～"
    assert res.options == ["2 人", "4 人", "一家人"]


def test_ready_ignores_llm_question(monkeypatch) -> None:
    """必填集齐即就绪：就绪分支恒不带追问，LLM 给了也不透传。"""
    raw = '{"city":"成都","days":3,"persons":2,"question":"还想问点什么","options":["a"]}'
    res = _run(monkeypatch, raw)
    assert res.ready is True
    assert res.question is None
    assert res.options == []


def test_llm_failure_flags_degraded_with_honest_copy(monkeypatch) -> None:
    """AILIVE-2：LLM 挂掉不再伪装成正常缺槽追问——降级标记 + 兜底话术必须透出，
    否则用户面对无提示的无限追问循环（2026-10-05 夜审实测 9/9 同模板）。"""
    res = _run(monkeypatch, RuntimeError("llm down"))
    assert res.ready is False
    assert res.blocked is False
    assert res.degraded is True
    assert res.question == clarify_mod._DEGRADED_NOTICE
    assert res.options == ["帮我推荐目的地"], "缺槽 chips 保留：服务恢复后点选即继续"
    assert res.missing == ["city", "days", "persons"]


def test_unparseable_llm_output_flags_degraded_and_keeps_old_slots(monkeypatch) -> None:
    """AILIVE-2：解析失败等同本轮没抽到，同样按降级透出；已入槽位不丢。"""
    res = _run(monkeypatch, "我觉得你说得对", slots={"city": "成都"})
    assert res.slots["city"] == "成都"
    assert res.missing == ["days", "persons"]
    assert res.ready is False
    assert res.degraded is True
    assert res.question == clarify_mod._DEGRADED_NOTICE


def test_overlong_days_blocks_instead_of_truncating(monkeypatch) -> None:
    raw = '{"city":"成都","days":14,"persons":2,"question":null,"options":null}'
    res = _run(monkeypatch, raw)
    assert res.blocked is True
    assert res.ready is False
    assert res.slots["days"] == 14, "上限不静默截断：天数原样保留，由对话协商"
    assert res.question is not None and "7 天" in res.question
    assert res.options == [f"改成 {MAX_TRIP_DAYS} 天以内", "拆成两段行程"]


def test_overlong_days_uses_llm_negotiation_copy(monkeypatch) -> None:
    raw = (
        '{"city":"成都","days":14,"persons":2,'
        '"question":"最多只能排 7 天哦，要不要拆成两段？","options":["拆两段","改 7 天"]}'
    )
    res = _run(monkeypatch, raw)
    assert res.blocked is True
    assert res.question is not None and res.question.startswith("最多只能排 7 天")
    assert res.options == ["拆两段", "改 7 天"]


def test_nonpositive_or_wordy_numbers_are_reasked(monkeypatch) -> None:
    raw = '{"city":"成都","days":0,"persons":"两周","budget":3000}'
    res = _run(monkeypatch, raw)
    assert "days" in res.missing and "persons" in res.missing
    assert res.ready is False
    assert "days" not in res.slots and "persons" not in res.slots


def test_options_capped_at_four(monkeypatch) -> None:
    """选项截顶只剩超天协商分支在用（缺槽分支的选项已改为按槽位现生成）。"""
    raw = '{"city":"成都","days":14,"question":"天数超了","options":["a","b","c","d","e"]}'
    res = _run(monkeypatch, raw)
    assert res.blocked is True
    assert len(res.options) == 4


# ---------- BIZ-4：矛盾预算前置提示 ----------


def test_absurd_budget_warns_once_before_ready(monkeypatch) -> None:
    """人均每天 < ¥100 时就绪前协商一次（blocked）；slots 标记往返后不再拦。"""
    raw = '{"city":"成都","days":7,"persons":2,"budget":500,"question":null,"options":null}'
    first = _run(monkeypatch, raw)
    assert first.ready is False and first.blocked is True
    assert first.question is not None and "人均每天" in first.question
    assert first.options == ["提高预算", "减少天数", "就按这个预算试试"]
    assert first.slots["_budget_warned"] is True, "标记随 slots 回传，前端下轮带回"
    # 第二轮（用户点「就按这个预算试试」，槽位不变）→ 不再拦，正常就绪
    second = _run(monkeypatch, '{"question":null,"options":null}', slots=dict(first.slots))
    assert second.ready is True and second.blocked is False


def test_tight_but_sane_budget_does_not_block(monkeypatch) -> None:
    """¥150/天 属节俭档但可执行：不得误拦（地板线 100，留出节俭空间）。"""
    raw = '{"city":"成都","days":2,"persons":2,"budget":600,"question":null,"options":null}'
    res = _run(monkeypatch, raw)
    assert res.ready is True and res.blocked is False


# ---------- 残留③：行程级荒谬预检（一天多城） ----------


def _stub_hits(monkeypatch, hits: list[str]) -> None:
    monkeypatch.setattr(clarify_mod, "known_city_hits", lambda text: hits)


def test_four_cities_one_day_warns_once(monkeypatch) -> None:
    """一天四城在就绪前协商一次；slots 标记往返后不再拦。"""
    _stub_hits(monkeypatch, ["北京", "上海", "广州", "深圳"])
    raw = '{"city":"北京上海广州深圳一日游","days":1,"persons":2,"question":null,"options":null}'
    first = _run(monkeypatch, raw)
    assert first.ready is False and first.blocked is True
    assert first.question is not None and "赶路" in first.question
    assert first.slots["_multicity_warned"] is True
    # 第二轮（用户点「就按一天多城试试」，槽位不变）→ 不再拦，正常就绪
    _stub_hits(monkeypatch, ["北京", "上海", "广州", "深圳"])
    second = _run(monkeypatch, '{"question":null,"options":null}', slots=dict(first.slots))
    assert second.ready is True and second.blocked is False


def test_two_cities_one_day_is_not_blocked(monkeypatch) -> None:
    """双城一日（苏杭经典玩法）不得误拦：命中数 2 未超线。"""
    _stub_hits(monkeypatch, ["苏州", "杭州"])
    raw = '{"city":"苏州杭州","days":1,"persons":1,"question":null,"options":null}'
    res = _run(monkeypatch, raw)
    assert res.ready is True and res.blocked is False


def test_multicity_only_blocks_single_day_trips(monkeypatch) -> None:
    """多天多城（环线游）不拦：判据必须同时满足 days=1。"""
    _stub_hits(monkeypatch, ["北京", "上海", "广州", "深圳"])
    raw = '{"city":"北京上海广州深圳","days":4,"persons":2,"question":null,"options":null}'
    res = _run(monkeypatch, raw)
    assert res.ready is True and res.blocked is False


def test_multicity_check_fails_open_without_dictionary(monkeypatch) -> None:
    """字典不可达（返回空命中）时启发式不触发：fail-open，不因 DB 抖动卡住 intake。"""
    _stub_hits(monkeypatch, [])
    raw = '{"city":"北京上海广州深圳","days":1,"persons":2,"question":null,"options":null}'
    res = _run(monkeypatch, raw)
    assert res.ready is True and res.blocked is False
