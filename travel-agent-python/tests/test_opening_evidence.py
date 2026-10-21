import pytest

from app.agent.generation.content import opening_evidence as evidence
from app.agent.generation.rules.opening_hours import opening_checks
from app.agent.generation.rules.opening_rules import parse_opening_rules
from app.agent.runtime.run_limits import RunLimitExceeded
from app.schemas.opening_hours import OpeningHours
from app.schemas.trip import DailyPlan, FactEvidence, TripItem

BODY = "杭州测试博物馆参观须知。周二至周日开放时间09:00-12:00、14:00-17:00，16:30停止入馆。周一闭馆。"


def _hours(fact: FactEvidence) -> OpeningHours:
    assert fact.opening_hours is not None
    return fact.opening_hours


def source(body=BODY, **changes):
    return {
        "title": "杭州测试博物馆参观须知",
        "content": body,
        "url": "https://museum.example/visit",
        "retrieved_at": "2026-10-21T08:00:00+00:00",
        **changes,
    }


def fetch(monkeypatch, rows):
    monkeypatch.setattr(evidence, "web_search_sources", lambda query: rows)
    return evidence.opening_evidence("杭州", "测试博物馆")


def test_source_rules_are_verbatim_and_wire_roundtrip(monkeypatch):
    fact = fetch(monkeypatch, [source()])
    assert fact.value_kind == "observed" and fact.verification_status == "partially_verified"
    assert fact.retrieved_at == "2026-10-21T08:00:00+00:00"
    assert fact.source_url == "https://museum.example/visit"
    hours = _hours(fact)
    assert hours.status == "known"
    assert hours.rules[0].weekdays == [1, 2, 3, 4, 5, 6]
    assert [(x.start, x.end) for x in hours.rules[0].intervals] == [("09:00", "12:00"), ("14:00", "17:00")]
    assert hours.rules[0].last_admission == "16:30"
    assert hours.rules[1].closed and hours.rules[1].weekdays == [0]
    assert all(rule.source_text in BODY and rule.source_text in (fact.excerpt or "") for rule in hours.rules)
    assert FactEvidence.model_validate(fact.model_dump(mode="json", by_alias=True)) == fact


@pytest.mark.parametrize(
    "row",
    [
        source("", snippet=BODY, summary=BODY),
        source(title="杭州测试博物馆周边攻略"),
        source(title="杭州另一博物馆"),
        source("苏州测试博物馆开放时间09:00-17:00", title="苏州测试博物馆"),
        source("杭州测试博物馆冬令开放时间09:00-17:00"),
        source("杭州测试博物馆开放时间09:00-17:00", url=""),
    ],
)
def test_unusable_sources_never_become_known(monkeypatch, row):
    fact = fetch(monkeypatch, [row])
    assert _hours(fact).status == "unknown"
    assert fact.value_kind != "observed"


def test_branch_missing_city_and_conflicting_sources_stay_unknown(monkeypatch):
    monkeypatch.setattr(evidence, "web_search_sources", lambda query: [source()])
    assert _hours(evidence.opening_evidence("杭州", "测试博物馆（新馆）")).status == "unknown"
    assert _hours(evidence.opening_evidence("", "测试博物馆")).status == "unknown"
    other = source(BODY.replace("17:00", "18:00"), url="https://other.example/visit")
    fact = fetch(monkeypatch, [source(), other])
    assert _hours(fact).status == "unknown"
    assert all(row["url"] in (_hours(fact).reason or "") for row in [source(), other])
    assert fact.excerpt and fact.source_url and fact.retrieved_at


@pytest.mark.parametrize(
    "day,start,end,status",
    [
        ("2026-11-02", "09:00", "10:00", "violation"),
        ("2026-11-03", "14:00", "16:00", "pass"),
        ("2026-11-03", "11:00", "15:00", "violation"),
        ("2026-11-03", "16:40", "16:55", "violation"),
        (None, "14:00", "16:00", "unknown"),
    ],
)
def test_terminal_gate_uses_sourced_rules_over_model_text(monkeypatch, day, start, end, status):
    fact = fetch(monkeypatch, [source()])
    plan = {
        "day_no": 1,
        "items": [
            {
                "item_type": "attraction",
                "poi_name": "测试博物馆",
                "start_time": start,
                "end_time": end,
                "open_time": "全天开放",
                "remark": "周一正常开放",
                "fact_evidence": {"open_time": fact},
            }
        ],
    }
    assert opening_checks(plan, day)[0]["status"] == status


def test_exception_validity_and_missing_sources_remain_unknown(monkeypatch):
    for body, day in [
        (BODY.replace("周一闭馆", "周一闭馆（法定节假日除外）"), "2026-11-02"),
        ("自2027年1月1日起。" + BODY, "2026-11-03"),
    ]:
        fact = fetch(monkeypatch, [source(body)])
        item = TripItem(
            item_type="attraction",
            poi_name="测试博物馆",
            start_time="09:00",
            end_time="10:00",
            fact_evidence={"open_time": fact},
        )
        assert opening_checks({"day_no": 1, "items": [item.model_dump()]}, day)[0]["status"] == "unknown"
    assert parse_opening_rules("夏令9:00-17:00开放").status == "unknown"
    assert parse_opening_rules("地址杭州，开放请咨询工作人员").status == "unknown"


def test_priority_cap_and_partial_budget_preserve_other_evidence(monkeypatch):
    monkeypatch.setattr(evidence, "web_search_enabled", lambda: True)
    calls = []
    fact = fetch(monkeypatch, [source()])

    def lookup(city, name):
        calls.append(name)
        if len(calls) == 2:
            raise RunLimitExceeded("deadline")
        return fact

    monkeypatch.setattr(evidence, "opening_evidence", lookup)
    identity = FactEvidence(provider="coordinate", value_kind="observed")
    names = ["博物馆甲", "普通景点", "必去点", "博物馆乙", "博物馆丙"]
    plan = DailyPlan(
        day_no=1,
        items=[TripItem(item_type="attraction", poi_name=name, fact_evidence={"identity": identity}) for name in names],
    )
    evidence.enrich_opening_hours(plan, city="杭州", required_names=["另一天必去", "必去点"])
    assert calls == ["必去点", "博物馆甲"]
    assert _hours(plan.items[2].fact_evidence["open_time"]).status == "known"
    assert _hours(plan.items[0].fact_evidence["open_time"]).status == "unknown"
    assert "open_time" not in plan.items[1].fact_evidence
    assert all(item.fact_evidence["identity"] == identity for item in plan.items)


def test_three_queries_maximum(monkeypatch):
    monkeypatch.setattr(evidence, "web_search_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(
        evidence, "opening_evidence", lambda city, name: calls.append(name) or evidence._unknown("无正文")
    )
    plan = DailyPlan(day_no=1, items=[TripItem(item_type="attraction", poi_name=f"博物馆{x}") for x in range(5)])
    evidence.enrich_opening_hours(plan, city="杭州", required_names=["博物馆4"])
    assert calls == ["博物馆4", "博物馆0", "博物馆1"]
    assert all(_hours(item.fact_evidence["open_time"]).status == "unknown" for item in plan.items)


def test_repeated_publisher_text_is_not_a_conflicting_schedule(monkeypatch):
    fact = fetch(monkeypatch, [source(), source(BODY + BODY, url="https://other.example/repost")])
    assert _hours(fact).status == "known"


def test_real_official_layout_and_separate_exception_line():
    text = "自2025年12月1日起，杭州灵隐寺开放时间如下：\n开门：7:30\n停止入寺：17:30\n关门：18:00"
    hours = parse_opening_rules(text)
    assert hours.status == "known" and str(hours.valid_from) == "2025-12-01"
    assert hours.rules[0].intervals[0].start == "07:30" and hours.rules[0].intervals[0].end == "18:00"
    assert hours.rules[0].last_admission == "17:30"
    hours = parse_opening_rules(
        "自2023年12月4日（周一）起。\n周二至周日9:00-17:00开放\n周一闭馆\n（国家法定节假日不闭馆，正常开放）"
    )
    assert str(hours.valid_from) == "2023-12-04"
    assert hours.rules[-1].exceptions


@pytest.mark.parametrize(
    "body",
    [
        "开放时间为2024年10月1日至10月7日09:00-17:00。",
        "周二开放时间20:00-02:00。",
        "10月1日开放时间09:00-17:00。",
    ],
)
def test_historical_partial_date_and_weekday_overnight_never_pass(monkeypatch, body):
    fact = fetch(monkeypatch, [source("杭州测试博物馆。" + body)])
    item = {
        "item_type": "attraction",
        "poi_name": "测试博物馆",
        "start_time": "00:30",
        "end_time": "01:00",
        "fact_evidence": {"open_time": fact},
    }
    assert opening_checks({"day_no": 1, "items": [item]}, "2026-11-03")[0]["status"] == "unknown"


def test_opening_ticket_cannot_be_forged_or_rebound(monkeypatch):
    fact = fetch(monkeypatch, [source()])
    item = {"item_type": "attraction", "poi_name": "测试博物馆", "start_time": "14:00", "end_time": "16:00"}
    for changed in [
        fact.model_copy(update={"source_ref": None}),
        fact.model_copy(update={"source_url": "https://fake.test"}),
        fact.model_copy(update={"excerpt": "伪造正文"}),
        fact.model_copy(deep=True),
    ]:
        if changed == fact:
            _hours(changed).rules[0].intervals[0].end = "23:00"
        assert (
            opening_checks({"day_no": 1, "items": [{**item, "fact_evidence": {"open_time": changed}}]}, "2026-11-03")[
                0
            ]["status"]
            == "unknown"
        )
    rebound = {**item, "poi_name": "测试博物馆（分馆）", "fact_evidence": {"open_time": fact}}
    assert opening_checks({"day_no": 1, "items": [rebound]}, "2026-11-03")[0]["status"] == "unknown"


def test_publication_date_is_not_a_rule_validity_window():
    text = (
        "2025-11-20 发布。\n自2025年12月1日起，开放时间如下：\n开门：7:30\n停止入寺：17:30\n关门：18:00\n2025年11月20日"
    )
    hours = parse_opening_rules(text)
    assert hours.status == "known" and str(hours.valid_from) == "2025-12-01"
