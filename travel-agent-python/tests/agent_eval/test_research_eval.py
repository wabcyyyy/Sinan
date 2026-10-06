"""多 Agent 研究编排评测的确定性回归测试（fixture 模式，零外部依赖）。"""

from tests.agent_eval import eval_research


def test_research_eval_reports_per_domain_packs():
    report = eval_research.build_report("杭州")
    assert {d["domain"] for d in report["domains"]} == {"attraction", "food", "hotel"}
    by_domain = {d["domain"]: d for d in report["domains"]}
    assert by_domain["attraction"]["count"] == 12
    assert by_domain["food"]["count"] == 4
    assert by_domain["hotel"]["count"] == 2
    assert all(d["rounds"] >= 1 for d in report["domains"])
    assert all(not d["degraded"] for d in report["domains"])


def test_research_eval_refill_effectiveness():
    report = eval_research.build_report("杭州")
    refill = report["refill"]
    assert refill["first_round_empty"] is True
    assert refill["refill_filled"] is True
    assert refill["refill_items"] == 1
    assert report["metrics"]["refill_effectiveness"] == 1.0
