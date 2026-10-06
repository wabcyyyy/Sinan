from tests.agent_eval.eval_agent import build_report


def test_offline_agent_evaluation_covers_workflow_and_authority():
    report = build_report([{"city": "杭州", "days": 2, "persons": 2, "preferences": ["自然风光"]}])
    metrics = report["metrics"]
    assert metrics["poi_authority_rate"] == 1.0
    assert metrics["field_reference_rate"] == 1.0
    assert metrics["time_conflict_rate"] == 0.0
    assert metrics["route_violation_rate"] == 0.0
    assert metrics["attraction_duplicate_rate"] == 0.0
    assert metrics["budget_deviation_rate"] == 0.0
    assert metrics["success_status_rate"] == 0.0
    assert metrics["degraded_status_rate"] == 1.0
    assert metrics["failed_status_rate"] == 0.0
    assert metrics["fallback_success_rate"] == 1.0
    assert metrics["trace_complete_rate"] == 1.0


def test_authority_is_evidence_based_not_directory_membership():
    """D11A：断掉"出题的表给自己打分"的回路。

    过去 `poi_authority_rate` 数的是"名字在 fixture 目录里"，mock 评测恒 100%，
    指标什么都证明不了。现在认的是**本服务签发的证据票**：同样两个目录内的名字，
    没票的那个不再算权威——真实 LLM 自选点位就落在这一档。
    """
    from app.agent.grounding.grounding_evidence import issue_evidence
    from app.common import cache_store
    from app.schemas.trip import DailyPlan, GenerateResponse, TripItem
    from tests.agent_eval import mock_llm
    from tests.agent_eval.metrics import evaluate_response

    cache_store.reset_for_tests()
    # 直接用 _attractions 构造目录（catalog() 会给每一行签票，这里要的就是
    # "一行有票、一行没票"的对照）
    rows = mock_llm._attractions("杭州")
    catalog = {"attractions": rows, "foods": [], "hotels": [], "consumption": {"meal_price": 60}}
    signed, unsigned = rows[0]["name"], rows[1]["name"]
    issue_evidence({**rows[0], "city": "杭州"})

    response = GenerateResponse(
        city="杭州",
        days=1,
        title="t",
        daily_plans=[
            DailyPlan(
                day_no=1,
                items=[
                    TripItem(item_type="attraction", poi_name=signed),
                    TripItem(item_type="attraction", poi_name=unsigned),
                ],
            )
        ],
    )
    result = evaluate_response(response, {"city": "杭州", "days": 1, "persons": 1}, catalog, {"events": []})
    assert result["poi_authority_rate"] == 0.5, "两个目录内的名字都算权威 = 自证回路没断"
    assert result["poi_grounded_rate"] == 0.0  # 票有了但这一版没落坐标 → 两个指标要成对读
