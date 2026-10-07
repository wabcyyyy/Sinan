from unittest.mock import patch

from app.agent.generation.content import landing
from app.agent.generation.orchestration import open_plans, workflow
from app.agent.research import reasoning
from app.agent.tools import impl as tools
from app.schemas.trip import GenerateRequest
from tests.agent_eval import mock_llm


def _patch_catalog():
    return (
        patch.object(tools, "search_attractions", mock_llm.search_attractions),
        patch.object(tools, "search_foods", mock_llm.search_foods),
        patch.object(tools, "get_consumption", mock_llm.get_consumption),
        patch.object(tools, "search_hotels", mock_llm.search_hotels),
        patch.object(reasoning, "plan_research", mock_llm.plan_research),
        patch.object(reasoning, "evaluate_research", mock_llm.evaluate_research),
        patch.object(tools, "attach_poi_images", mock_llm.attach_poi_images),
    )


def test_missing_llm_returns_draft_not_fake_itinerary(monkeypatch):
    """LLM-only 口径：未配置 LLM 时返回待研究草案，绝不以知识库拼装行程。"""
    monkeypatch.setattr(workflow.settings, "llm_api_key", "")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)
    patches = _patch_catalog()
    for item in patches:
        item.start()
    try:
        response = workflow.run_generate(GenerateRequest(city="杭州", days=1))
    finally:
        for item in reversed(patches):
            item.stop()
    assert response.status == "failed"
    assert response.destination_status == "draft_only"
    assert "未配置 LLM" in (response.status_reason or "")
    assert all(not day.items for day in response.daily_plans)


def test_valid_llm_generation_is_delivered_with_review_notice(monkeypatch):
    """开放模式合法行程正常交付；因含模型自选点，如实标注出发前复核。"""
    monkeypatch.setattr(workflow.settings, "llm_api_key", "configured")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)

    def valid_open_day(req, _used):
        # 通过当前 reflect：活动时长 ≥240、含餐饮、相邻间隔足够
        return {
            "note": f"{req.city}行程",
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "杭州景点1",
                    "start_time": "09:00",
                    "end_time": "12:00",
                    "duration_min": 180,
                    "latitude": 30.0,
                    "longitude": 120.0,
                    "cost": 25,
                },
                {
                    "item_type": "food",
                    "poi_name": "杭州本地餐厅1",
                    "start_time": "12:40",
                    "end_time": "13:40",
                    "duration_min": 60,
                    "latitude": 30.01,
                    "longitude": 120.01,
                    "cost": 70,
                },
                {
                    "item_type": "attraction",
                    "poi_name": "杭州景点2",
                    "start_time": "14:10",
                    "end_time": "16:10",
                    "duration_min": 120,
                    "latitude": 30.02,
                    "longitude": 120.02,
                    "cost": 30,
                },
                {
                    "item_type": "hotel",
                    "poi_name": "杭州舒适酒店1",
                    "cost": 350,
                    "start_time": "18:00",
                    "end_time": "18:30",
                    "duration_min": 30,
                    "latitude": 30.05,
                    "longitude": 120.05,
                },
            ],
        }

    patches = _patch_catalog()
    for item in patches:
        item.start()
    with patch.object(open_plans, "llm_open_day", valid_open_day):
        try:
            response = workflow.run_generate(GenerateRequest(city="杭州", days=1))
        finally:
            for item in reversed(patches):
                item.stop()
    assert response.status == "degraded"
    assert "开放研究" in (response.status_reason or "")
    assert response.quality_report.quality_status == "READY_WITH_WARNINGS"
    names = [item.poi_name for day in response.daily_plans for item in day.items]
    # BIZ-6（2026-10-06 改基线）：wire 响应与落库同口径——N 天住 N-1 晚，
    # 1 天行程（0 晚）不保留酒店项，此前 wire 多出的「杭州舒适酒店1」即口径分裂本体
    assert names == ["杭州景点1", "杭州本地餐厅1", "杭州景点2"]


def test_final_validation_marks_unfixed_constraints_as_degraded(monkeypatch):
    monkeypatch.setattr(workflow.settings, "llm_api_key", "configured")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)

    def invalid_open_day(req, _used):
        return {
            "note": f"{req.city}行程",
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "杭州景点1",
                    "start_time": "17:00",
                    "end_time": "19:00",
                    "duration_min": 120,
                    "latitude": 30.0,
                    "longitude": 120.0,
                    "cost": 25,
                },
            ],
        }

    patches = _patch_catalog()
    for item in patches:
        item.start()
    with patch.object(open_plans, "llm_open_day", invalid_open_day):
        try:
            response = workflow.run_generate(GenerateRequest(city="杭州", days=1))
        finally:
            for item in reversed(patches):
                item.stop()
    assert response.status == "degraded"
    assert "行程约束问题" in (response.status_reason or "")
    assert "保留" in (response.status_reason or "")  # LLM-only：保留结果而非替换
    assert any("开放时间不符" in entry for entry in response.validation_log)


def test_unknown_destination_without_candidates_is_draft_only(monkeypatch):
    """候选池三域全空 = 没有任何外部证据，不得自称 researched（PLAN-A1 P4）。

    曾经这里断言 researched：OTM key 未配 / 城市中心缺失 / 联网预算耗尽都会
    把池打成空且不算 error，于是"模型凭自身知识写完"与"查过外部数据"共用
    同一个状态，前端与导出都读不出差别。
    """
    monkeypatch.setattr(workflow.settings, "llm_api_key", "configured")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)
    monkeypatch.setattr(tools, "search_attractions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "search_foods", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "get_consumption", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tools, "search_hotels", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(reasoning, "plan_research", mock_llm.plan_research)
    monkeypatch.setattr(reasoning, "evaluate_research", mock_llm.evaluate_research)
    monkeypatch.setattr(landing, "local_ground", lambda *_args, **_kwargs: None)

    def open_day(req, _used):
        return {
            "note": f"{req.city}体验日",
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": f"{req.city}地标",
                    "start_time": "09:00",
                    "end_time": "12:00",
                    "duration_min": 180,
                    "latitude": 30.0,
                    "longitude": 120.0,
                    "cost": 50,
                },
                {
                    "item_type": "food",
                    "poi_name": f"{req.city}餐厅",
                    "start_time": "12:40",
                    "end_time": "13:40",
                    "duration_min": 60,
                    "latitude": 30.01,
                    "longitude": 120.01,
                    "cost": 60,
                },
                {
                    "item_type": "attraction",
                    "poi_name": f"{req.city}公园",
                    "start_time": "14:10",
                    "end_time": "16:10",
                    "duration_min": 120,
                    "latitude": 30.02,
                    "longitude": 120.02,
                    "cost": 0,
                },
            ],
        }

    with patch.object(open_plans, "llm_open_day", open_day):
        response = workflow.run_generate(GenerateRequest(city="不存在的目的地", days=1))

    assert response.destination_status == "draft_only"
    assert response.schedule_report["open_research"] is True
    assert response.schedule_report["empty_domains"] == ["candidates", "foods", "hotels"]
    assert response.quality_report.quality_status == "READY_WITH_WARNINGS"
    item = response.daily_plans[0].items[0]
    assert item.source == "llm.open_day"
    assert item.verification_status == "unverified"


def test_unknown_destination_without_llm_is_blocked_draft(monkeypatch):
    """无本地候选且无模型时返回结构化草案，并禁止误当成可交付行程。"""
    monkeypatch.setattr(workflow.settings, "llm_api_key", "")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)
    monkeypatch.setattr(tools, "search_attractions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "search_foods", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "get_consumption", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tools, "search_hotels", lambda *_args, **_kwargs: [])

    response = workflow.run_generate(GenerateRequest(city="不存在的目的地", days=2))

    assert response.destination_status == "draft_only"
    assert response.status == "failed"
    assert response.quality_report.quality_status == "BLOCKED"
    assert any(issue.code == "NO_ITINERARY_ITEMS" for issue in response.quality_report.blocking_issues)


def test_open_result_kept_when_validation_exhausted_without_candidates(monkeypatch):
    """知识库外目的地校验耗尽时保留开放模式结果，禁止降级为占位酒店行程。

    丽江事故回归：开放模式行程校验不过 → 旧逻辑 fallback_generate 在无候选时
    产出 0 景点的占位酒店行程并以 READY_WITH_WARNINGS 交付。
    """
    monkeypatch.setattr(workflow.settings, "llm_api_key", "configured")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)
    monkeypatch.setattr(tools, "search_attractions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "search_foods", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "get_consumption", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tools, "search_hotels", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(reasoning, "plan_research", mock_llm.plan_research)
    monkeypatch.setattr(reasoning, "evaluate_research", mock_llm.evaluate_research)
    monkeypatch.setattr(landing, "local_ground", lambda *_args, **_kwargs: None)

    def open_day(req, _used):
        # 只排酒店不排景点：制造"未安排任何景点"校验问题，且修复轮同样不过。
        return {
            "note": f"{req.city}行程",
            "items": [
                {
                    "item_type": "hotel",
                    "poi_name": "丽江悦榕庄",
                    "start_time": "21:00",
                    "end_time": "08:00",
                    "duration_min": 660,
                    "cost": 2500,
                },
            ],
        }

    with patch.object(open_plans, "llm_open_day", open_day):
        response = workflow.run_generate(GenerateRequest(city="丽江", days=1))

    # 核心断言：不出现 fallback 占位酒店（"丽江市区舒适酒店"）。
    # BIZ-6（2026-10-06 改基线）：1 天 0 晚口径下 wire 与落库同参——酒店项不保留，
    # 交付为空日 + BLOCKED（真实开放输出被如实保留，不被占位行程顶替）；
    # 刻意保持 days=1：days>1 会走 llm_open_trip 整段路径（本测试只 mock 逐日），
    # 全量套件下会吃到前序测试遗留的全局 LLM 桩（实测顺序污染）。
    names = [item.poi_name for day in response.daily_plans for item in day.items]
    assert names == []
    assert "丽江市区舒适酒店" not in names
    assert "保留" in (response.status_reason or "")
    assert response.quality_report.quality_status == "BLOCKED"
    # 空日交付的阻断码是 NO_ITINERARY_ITEMS（0 条目）；NO_ATTRACTION_ITEMS 只在
    # 「有条目但 0 景点」（占位酒店式）时触发
    assert any(issue.code == "NO_ITINERARY_ITEMS" for issue in response.quality_report.blocking_issues)


def test_open_result_with_attractions_kept_when_route_validation_exhausted(monkeypatch):
    """开放模式行程有景点但路线校验反复不过：保留原行程并如实降级，不整体作废。"""
    monkeypatch.setattr(workflow.settings, "llm_api_key", "configured")
    monkeypatch.setattr(workflow.settings, "live_price_search", False)
    monkeypatch.setattr(tools, "search_attractions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "search_foods", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "get_consumption", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tools, "search_hotels", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(reasoning, "plan_research", mock_llm.plan_research)
    monkeypatch.setattr(reasoning, "evaluate_research", mock_llm.evaluate_research)
    monkeypatch.setattr(landing, "local_ground", lambda *_args, **_kwargs: None)

    def open_day(req, _used):
        # 相邻 1 景点+餐饮+酒店，制造**修不掉**的路线类校验问题：间隔过短
        # （1 分钟 << 需约 123 分钟），且落在转场微调规则（rules/transfer_time）
        # 的边界外——前条已在 30 分钟游览保底无从收缩、后条贴着当天 23:59
        # 无顺延空间。可修复的缺口在生成后就地修掉了（2026-10-08），只有这种
        # 边界外的违规才会走到「路线校验反复不过 → 保留原行程并如实降级」。
        return {
            "note": f"{req.city}行程",
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "玉龙雪山",
                    "start_time": "19:00",
                    "end_time": "19:30",
                    "duration_min": 30,
                    "latitude": 27.1164,
                    "longitude": 100.18,
                    "cost": 100,
                },
                {
                    "item_type": "food",
                    "poi_name": "云雪丽餐厅",
                    "start_time": "19:31",
                    "end_time": "23:59",
                    "duration_min": 268,
                    "latitude": 26.87,
                    "longitude": 100.23,
                    "cost": 80,
                },
                {
                    "item_type": "hotel",
                    "poi_name": "丽江安麓",
                    "start_time": "21:00",
                    "end_time": "08:00",
                    "duration_min": 660,
                    "cost": 2500,
                },
            ],
        }

    with patch.object(open_plans, "llm_open_day", open_day):
        response = workflow.run_generate(GenerateRequest(city="丽江", days=1))

    names = [item.poi_name for day in response.daily_plans for item in day.items]
    assert "玉龙雪山" in names  # 景点保留，未被占位行程替换
    assert "丽江市区舒适酒店" not in names
    assert "保留" in (response.status_reason or "")
