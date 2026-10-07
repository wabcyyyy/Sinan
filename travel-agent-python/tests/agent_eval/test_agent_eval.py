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


def _one_day(city: str, persons: int, items: list[dict], consumption: dict) -> tuple:
    """一份单日行程 + 与其一致的评测目录，供预算/路线口径用例复用。"""
    from app.agent.generation.rules.generation_core import estimate_plans_total
    from app.schemas.trip import DailyPlan, GenerateResponse, TripItem

    plans = [DailyPlan(day_no=1, items=[TripItem(**item) for item in items], theme="t")]
    budget = estimate_plans_total([plan.model_dump() for plan in plans], persons, 1, consumption)
    response = GenerateResponse(city=city, days=1, title="t", daily_plans=plans, budget_estimate=budget)
    case = {"city": city, "days": 1, "persons": persons}
    catalog = {"attractions": [], "foods": [], "hotels": [], "consumption": consumption}
    return response, case, catalog, budget


def test_budget_metric_does_not_double_count_the_total_key():
    """预算指标不得把「合计」与四项明细各加一遍。

    `estimate_plans_total` 返回 5 键（四个类目 + 合计便捷键，generation_core.py:308-314），
    而 `GenerateResponse.budget_estimate` 在生产侧由 recompute_budget 只写四个类目
    （prices.py:134-154）。评测路径喂的是 5 键形状，裸 sum(values()) 会重复计数——
    实测一份**完全正确**的预算被判 100% 偏差，指标恒红、失去分辨力。
    """
    from tests.agent_eval.metrics import evaluate_response

    items = [
        {
            "item_type": "attraction",
            "poi_name": "故宫",
            "cost": 60,
            "latitude": 39.9,
            "longitude": 116.4,
            "start_time": "09:00",
            "end_time": "11:00",
        },
        {
            "item_type": "food",
            "poi_name": "餐厅",
            "cost": 90,
            "latitude": 39.9,
            "longitude": 116.4,
            "start_time": "12:00",
            "end_time": "13:00",
        },
    ]
    consumption = {"transport_price": 50, "meal_price": 90}
    response, case, catalog, budget = _one_day("北京", 2, items, consumption)

    assert "合计" in budget, "本用例的前提就是 5 键形状"
    result = evaluate_response(response, case, catalog, {"events": []})
    assert result["budget_deviation_rate"] == 0.0, "预算自报值与实际选中项一致，偏差必须是 0"


def test_budget_metric_still_sums_production_four_key_shape():
    """生产形状（无「合计」键）仍按四项求和——修复不能只认 5 键。"""
    from tests.agent_eval.metrics import evaluate_response

    items = [
        {
            "item_type": "attraction",
            "poi_name": "西湖",
            "cost": 0,
            "latitude": 30.25,
            "longitude": 120.15,
            "start_time": "09:00",
            "end_time": "11:30",
        },
        {
            "item_type": "food",
            "poi_name": "楼外楼",
            "cost": 120,
            "latitude": 30.25,
            "longitude": 120.15,
            "start_time": "12:00",
            "end_time": "13:30",
        },
    ]
    consumption = {"transport_price": 35, "meal_price": 80}
    response, case, catalog, budget = _one_day("杭州", 1, items, consumption)
    response.budget_estimate = {key: value for key, value in budget.items() if key != "合计"}

    result = evaluate_response(response, case, catalog, {"events": []})
    assert result["budget_deviation_rate"] == 0.0, "四键形状求和口径不能退化"


def test_route_violation_uses_production_tolerance():
    """路线判定必须带上生产判官的估算容差，否则把裕量差记成违规。

    `validate_plans` 对坐标估算路线给 ROUTE_ESTIMATE_TOLERANCE_MIN 分钟容差
    （reflect.py:156-161）；评测原先用零容差裸比，实测同一份行程被高估为
    50%-100% 违规。这里钉住两条：容差内不判、超出容差仍判。
    """
    from app.agent.data.route_service import ROUTE_ESTIMATE_TOLERANCE_MIN
    from app.agent.generation.rules.transfer_time import estimate_transfer_minutes
    from tests.agent_eval.metrics import evaluate_response

    consumption = {"transport_price": 35, "meal_price": 80}
    first = {
        "item_type": "attraction",
        "poi_name": "A",
        "cost": 0,
        "latitude": 30.25,
        "longitude": 120.15,
        "start_time": "09:00",
        "end_time": "11:00",
    }
    second = {
        "item_type": "attraction",
        "poi_name": "B",
        "cost": 0,
        "latitude": 30.30,
        "longitude": 120.20,
        "start_time": "11:15",
        "end_time": "13:00",
    }
    required = estimate_transfer_minutes(first, second)
    assert required is not None and required > ROUTE_ESTIMATE_TOLERANCE_MIN + 10, "本用例前提：需求换乘时间明显大于容差"

    def _clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    prev_end = 11 * 60  # 第一个点 09:00-11:00，留白从 11:00 起算

    # 留白 = required - 3：落在容差（5 分钟）内 → 不算违规
    forgiven_item = dict(second, start_time=_clock(prev_end + required - 3), end_time=_clock(prev_end + required + 117))
    response, case, catalog, _budget = _one_day("杭州", 1, [first, forgiven_item], consumption)
    forgiven = evaluate_response(response, case, catalog, {"events": []})
    assert forgiven["route_violation_rate"] == 0.0, "容差内的裕量差不应记成违规"

    # 留白 = required - 20：远超容差 → 仍判违规（容差不是免死金牌）
    tight_item = dict(second, start_time=_clock(prev_end + required - 20), end_time=_clock(prev_end + required + 100))
    response_tight, case_tight, catalog_tight, _ = _one_day("杭州", 1, [first, tight_item], consumption)
    strict = evaluate_response(response_tight, case_tight, catalog_tight, {"events": []})
    assert strict["route_violation_rate"] == 1.0, "真实不足必须判出来，容差不能吃掉它"


def test_terminal_reset_reasons_label_route_gap():
    """终检重置的成因必须认得「路线时间不足」。

    这条是实测踩出来的：标签表曾把短标签与匹配串写反（写成 ("路线时间不足", "转场时间不足")），
    于是「路线时间不足」这条规则永远匹配不上，归因里出现「重置了但成因是空」——
    真实数据里它恰恰是主要成因（上海第 1 天连续三对点位只留 10 分钟、需 17 分钟）。
    """
    from app.agent.generation.rules.transfer_time import estimate_transfer_minutes
    from tests.agent_eval.metrics import evaluate_response

    consumption = {"transport_price": 35, "meal_price": 80}
    first = {
        "item_type": "attraction",
        "poi_name": "A",
        "cost": 0,
        "latitude": 30.25,
        "longitude": 120.15,
        "start_time": "09:00",
        "end_time": "13:00",
    }
    second = {
        "item_type": "attraction",
        "poi_name": "B",
        "cost": 0,
        "latitude": 30.30,
        "longitude": 120.20,
        "start_time": "13:01",
        "end_time": "17:01",
    }
    assert estimate_transfer_minutes(first, second) is not None

    response, case, catalog, _budget = _one_day("杭州", 1, [first, second], consumption)
    result = evaluate_response(response, case, catalog, {"events": []})

    assert result["terminal_reset_day_rate"] == 1.0, "该天路线时间不足，生产会重置它"
    assert "转场时间不足" in result["terminal_reset_reasons"], (
        f"路线时间不足这条规则没被认出来：{result['terminal_reset_reasons']}"
    )


def test_unlabeled_kind_normalizes_unknown_rules():
    """兜底归一：标签表没认领的 issue 也要能聚合成可读标签，不许静默漏归因。"""
    from tests.agent_eval.metrics import _unlabeled_kind

    assert _unlabeled_kind("第 3 天新增了某条规则（约 12 个，上限 5）") == "新增了某条规则"
    assert _unlabeled_kind("第 1 天某规则：「某点位」不合规；请处理") == "某规则"
    assert _unlabeled_kind("") == "其他"
