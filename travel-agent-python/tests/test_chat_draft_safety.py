import json

from app.agent.editing.chat_draft import decide
from app.agent.editing.chat_draft.hotel_intent import (
    HotelIntent,
    _fallback_hotel_intent,
    _hotel_comparison_base_tier,
    _with_stay_scope,
)
from app.agent.editing.chat_draft.intent import (
    _increase_target_days,
    _is_time_adjustment_request,
    _is_vague_poi_browse_request,
    _reduce_target_days,
    _requested_day_count,
)
from app.agent.editing.chat_draft.plan_edit import (
    _apply_decision_patches,
    _apply_plan_update,
    _dedupe_plans,
    _deterministic_reduce,
)
from app.agent.editing.chat_draft.validate import (
    _decision_reply,
    _plan_conflict,
    _substantive_plan_signature,
)
from app.schemas.trip import ChatTurnRequest


def _request(message: str) -> ChatTurnRequest:
    return ChatTurnRequest(
        city="杭州",
        days=2,
        persons=2,
        plans=[
            {
                "day_no": 1,
                "note": "第一天",
                "items": [
                    {
                        "id": 1,
                        "item_type": "attraction",
                        "poi_name": "苏堤春晓",
                        "start_time": "08:30",
                        "end_time": "10:00",
                        "duration_min": 90,
                    },
                    {
                        "id": 2,
                        "item_type": "hotel",
                        "poi_name": "杭州西子宾馆汪庄",
                        "start_time": "20:00",
                        "duration_min": 30,
                    },
                ],
            },
            {
                "day_no": 2,
                "note": "第二天",
                "items": [
                    {
                        "id": 3,
                        "item_type": "attraction",
                        "poi_name": "灵隐寺",
                        "start_time": "07:00",
                        "end_time": "09:00",
                        "duration_min": 120,
                    }
                ],
            },
        ],
        message=message,
    )


def test_vague_poi_browse_does_not_imply_mutation():
    assert _is_vague_poi_browse_request("我想看看其他景点")
    assert not _is_vague_poi_browse_request("用岳王庙替换这个景点")


def test_time_update_recalculates_end_time():
    request = _request("把苏堤春晓改到下午三点")
    plans = _apply_decision_patches(
        {"target_days": None, "patches": [{"op": "update", "item_id": 1, "fields": {"start_time": "15:00"}}]},
        request,
    )
    item = next(item for item in plans[0]["items"] if item["id"] == 1)
    assert item["end_time"] == "16:30"


def test_unrequested_delete_is_ignored_when_extending_trip():
    request = _request("改成3天")
    plans = _apply_decision_patches(
        {
            "target_days": 3,
            "patches": [
                {"op": "delete", "item_id": 1},
                {"op": "move", "item_id": 3, "day_no": 3},
            ],
        },
        request,
    )
    assert any(item.get("id") == 1 for plan in plans for item in plan["items"])


def test_moved_item_is_rescheduled_when_original_time_conflicts():
    request = _request("把灵隐寺移到第一天")
    plans = _apply_decision_patches(
        {"target_days": None, "patches": [{"op": "move", "item_id": 3, "day_no": 1}]},
        request,
    )
    moved = next(item for item in plans[0]["items"] if item.get("id") == 3)
    assert moved["start_time"] == "10:00"
    assert moved["end_time"] == "12:00"


def test_conflict_and_note_only_change_are_detectable():
    request = _request("调整一下")
    changed_notes = [dict(plan, note="新备注") for plan in request.plans]
    assert _substantive_plan_signature(changed_notes) == _substantive_plan_signature(request.plans)
    conflict_plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "attraction", "poi_name": "甲", "start_time": "09:00", "end_time": "11:00"},
                {"item_type": "food", "poi_name": "乙", "start_time": "10:30", "end_time": "12:00"},
            ],
        }
    ]
    assert _plan_conflict(conflict_plans) == (1, "甲", "乙")


def test_placeholder_reply_and_generic_hotel_change_are_normalized():
    assert _decision_reply("中文Markdown", "安全回复") == "安全回复"
    assert _fallback_hotel_intent("给我换酒店", "豪华型").action == "same"


def test_unspecified_hotel_scope_defaults_to_all_existing_nights():
    request = _request("换个酒店")
    request.plans[1]["items"].append(
        {
            "id": 4,
            "item_type": "hotel",
            "poi_name": "杭州西子宾馆汪庄",
        }
    )
    intent = _with_stay_scope(HotelIntent("same", "奢华型", "奢华型"), request, [])
    assert intent.requested_nights == 2
    assert intent.requested_day_nos == (1, 2)


def test_numbered_night_is_not_misread_as_night_count():
    request = _request("第二晚换酒店")
    request.plans[1]["items"].append(
        {
            "id": 4,
            "item_type": "hotel",
            "poi_name": "杭州西子宾馆汪庄",
        }
    )
    intent = _with_stay_scope(HotelIntent("same", "奢华型", "奢华型"), request, [])
    assert not intent.invalid_scope
    assert intent.requested_nights == 1
    assert intent.requested_day_nos == (2,)


def test_only_explicit_continuation_uses_previous_proposed_tier():
    request = _request("再便宜一点")
    request.history = [
        {
            "role": "ai",
            "content": "你当前是奢华型，本次提供 **3 家豪华型酒店** 供比较。",
        }
    ]
    assert _hotel_comparison_base_tier(request, []) == "豪华型"
    request.message = "看看其他酒店"
    assert _hotel_comparison_base_tier(request, []) == "舒适型"


def test_explicit_new_hotel_day_can_target_day_without_existing_hotel():
    request = _request("第五天住四季")
    request.days = 5
    request.plans.extend(
        [
            {"day_no": 3, "items": []},
            {"day_no": 4, "items": []},
            {"day_no": 5, "items": []},
        ]
    )
    intent = _with_stay_scope(HotelIntent("specific", "奢华型", "奢华型"), request, [])
    assert not intent.invalid_scope
    assert intent.requested_day_nos == (5,)


def test_reduce_by_days_is_parsed_as_subtraction():
    # “减少 2 天”应理解为在现有天数上减去 2 天，而不是改成 2 天。
    assert _requested_day_count("减少2天", current_days=5) == 3
    assert _requested_day_count("缩短一天", current_days=5) == 4
    # 范围表述取首个数字做保守减量，不再误判成“改成 2 天”。
    assert _requested_day_count("减少一两天的行程", current_days=5) == 4
    # “改成 N 天”仍按目标天数解析。
    assert _requested_day_count("改成3天", current_days=5) == 3


def test_reduce_target_days_only_for_reduce_by_phrase():
    request = _request("减少一些重复景点")
    assert _reduce_target_days(request) is None
    request.message = "缩短2天"
    request.days = 5
    assert _reduce_target_days(request) == 3


def test_deterministic_reduce_removes_cross_day_duplicates():
    request = _request("帮我减少一些重复景点")
    request.plans[0]["items"].append(
        {
            "id": 5,
            "item_type": "attraction",
            "poi_name": "灵隐寺",
            "start_time": "13:00",
            "end_time": "15:00",
        }
    )
    plans = _deterministic_reduce(request)
    names = [it.get("poi_name") for plan in plans for it in plan["items"] if it.get("item_type") == "attraction"]
    assert names.count("灵隐寺") == 1
    # 酒店不受影响。
    assert any(it.get("poi_name") == "杭州西子宾馆汪庄" for plan in plans for it in plan["items"])


def test_deterministic_reduce_shortens_days():
    request = _request("缩短2天")
    request.days = 5
    request.plans = [
        {
            "day_no": d,
            "note": f"第{d}天",
            "items": [
                {
                    "id": d,
                    "item_type": "attraction",
                    "poi_name": f"景点{d}",
                    "start_time": "09:00",
                    "end_time": "11:00",
                },
            ],
        }
        for d in range(1, 6)
    ]
    plans = _deterministic_reduce(request, target_days=3)
    assert [p["day_no"] for p in plans] == [1, 2, 3]


def test_add_day_is_parsed_as_increase_by():
    # “加一天/增加两天”应理解为在现有天数上加上 N 天，而不是改成 1 天。
    assert _requested_day_count("我想再加一天行程", current_days=5) == 6
    assert _requested_day_count("增加两天", current_days=5) == 7
    assert _increase_target_days(_request("加一天")) == 3  # 固定夹具 days=2 → 2+1=3
    # extension_requested 在 run_chat_turn 里正是用 _increase_target_days(req) is not None 判定
    assert _increase_target_days(_request("延长一天行程")) == 3
    assert _increase_target_days(_request("减少一些景点")) is None


def test_fullday_activity_phrase_counts_as_one_more_day():
    """活体 R1 取证（2026-10-06）：「加上<长活动名>的一整天」必须读出 +1 天，
    否则模型按 6 天重排会被 target_days=5 的元数据校验拒绝成『无法生成安全草稿』。"""
    assert _requested_day_count("加上东京迪士尼乐园的一整天", current_days=5) == 6
    assert _increase_target_days(_request("补上环球影城的一整天")) == 3
    # 无"的"表述不触发备选模式，避免「补货需要一整天」类歧义句被当成加天
    assert _requested_day_count("购物补货需要一整天", current_days=5) is None
    # 短句式仍走主正则
    assert _requested_day_count("加一天", current_days=5) == 6


def test_dedupe_plans_removes_cross_day_duplicates_without_touching_hotels():
    plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "10:00"},
                {"item_type": "hotel", "poi_name": "H"},
            ],
        },
        {
            "day_no": 2,
            "items": [
                {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "10:00"},
                {"item_type": "food", "poi_name": "B", "start_time": "12:00", "end_time": "13:00"},
            ],
        },
    ]
    out = _dedupe_plans(plans)
    names = [it["poi_name"] for p in out for it in p["items"]]
    assert names.count("A") == 1
    assert "H" in names
    assert "B" in names


def test_rewrite_plan_routes_full_document():
    # “减少一天并重新安排景点”这类大改应走 rewrite_plan，模型返回完整 plan_document，
    # 由 _extract_document_plans 安全落地（保留项带原 id、删除项不写入）。
    request = _request("减少一天并重新安排景点")  # 夹具 days=2 → 目标 1 天
    decision = {
        "mode": "rewrite_plan",
        "plan_document": {
            "schema_version": 1,
            "trip": {
                "city": "杭州",
                "days": 1,
                "persons": 2,
                "budget": None,
                "start_date": None,
                "end_date": None,
                "preferences": [],
                "hotel_tier": None,
            },
            "days": [
                {
                    "day_no": 1,
                    "note": "第一天",
                    "items": [
                        {
                            "id": 1,
                            "item_type": "attraction",
                            "poi_name": "苏堤春晓",
                            "start_time": "08:30",
                            "end_time": "10:00",
                            "duration_min": 90,
                        }
                    ],
                }
            ],
        },
    }
    plans = _apply_plan_update(decision, request)
    assert plans is not None
    assert len(plans) == 1
    assert plans[0]["items"][0]["poi_name"] == "苏堤春晓"


def test_rewrite_plan_minimal_echo_is_hydrated():
    """prompt 收紧后的轻量回显（2026-10-06 活体 R1 取证）：保留项只写 id+poi_name，
    未写出的字段由 hydrate 按现有计划回填——大行程不再因模型照抄全量字段超输出上限。"""
    request = _request("把灵隐寺挪到第一天，苏堤挪到第二天")
    decision = {
        "mode": "rewrite_plan",
        "plan_document": {
            "schema_version": 1,
            "trip": {
                "city": "杭州",
                "days": 2,
                "persons": 2,
                "budget": None,
                "start_date": None,
                "end_date": None,
                "preferences": [],
                "hotel_tier": None,
            },
            "days": [
                {"day_no": 1, "note": "第一天", "items": [{"id": 3, "poi_name": "灵隐寺"}]},
                {
                    "day_no": 2,
                    "note": "第二天",
                    "items": [
                        {"id": 1, "poi_name": "苏堤春晓"},
                        {"id": 2, "poi_name": "杭州西子宾馆汪庄"},
                    ],
                },
            ],
        },
    }
    plans = _apply_plan_update(decision, request)
    assert plans is not None
    lingyin = plans[0]["items"][0]
    assert lingyin["poi_name"] == "灵隐寺"
    assert lingyin["start_time"] == "07:00" and lingyin["end_time"] == "09:00", "未写字段按现有计划回填"
    uidamo = next(it for it in plans[1]["items"] if it.get("id") == 1)
    assert uidamo["start_time"] == "08:30", "重排项目的原有时间随 id 回填"


def test_plan_update_still_routes_patches():
    # 小修小补仍走 patches 补丁路径，不受 rewrite_plan 影响。
    request = _request("把苏堤春晓改到下午三点")
    decision = {"mode": "plan_update", "patches": [{"op": "update", "item_id": 1, "fields": {"start_time": "15:00"}}]}
    plans = _apply_plan_update(decision, request)
    assert plans is not None
    item = next(it for p in plans for it in p["items"] if it.get("id") == 1)
    assert item["start_time"] == "15:00"


def test_time_adjustment_request_detected():
    """2026-09-30 评审误路由项的守卫原语：挪动时段 ≠ 换住宿。"""
    assert _is_time_adjustment_request("把酒店挪到晚上，博物馆放上午")
    assert _is_time_adjustment_request("把入住改到下午")
    assert _is_time_adjustment_request("景点移到早上")
    assert not _is_time_adjustment_request("帮我换一个酒店")
    assert not _is_time_adjustment_request("酒店要好一点的")
    assert not _is_time_adjustment_request("把博物馆挪到西湖边上"), "有挪动但无时段词，不算时间调整"


def test_hotel_time_update_patch_is_applied():
    """酒店条目允许纯时间 update（身份字段不在 allowed_update_fields，改不了住宿本身）。"""
    request = _request("把酒店挪到晚上")
    plans = _apply_decision_patches(
        {"target_days": None, "patches": [{"op": "update", "item_id": 2, "fields": {"start_time": "21:00"}}]},
        request,
    )
    hotel = next(item for item in plans[0]["items"] if item["id"] == 2)
    assert hotel["start_time"] == "21:00"


def test_hotel_delete_and_move_patches_still_ignored():
    request = _request("把酒店删掉，别的重新安排")
    plans = _apply_decision_patches(
        {
            "target_days": None,
            "patches": [
                {"op": "delete", "item_id": 2},
                {"op": "move", "item_id": 2, "day_no": 2},
            ],
        },
        request,
    )
    assert any(item["id"] == 2 for item in plans[0]["items"]), "酒店 delete 仍被拒绝"
    assert not any(item["id"] == 2 for item in plans[1]["items"]), "酒店 move 仍被拒绝"


def _stub_decision(monkeypatch, decision: dict) -> None:
    monkeypatch.setattr(decide, "_decide_plan_change", lambda req, hotels, feedback=None: decision)
    monkeypatch.setattr(decide.tools, "search_hotels", lambda *args, **kwargs: [])


def test_time_adjustment_is_not_hijacked_into_hotel_flow(monkeypatch):
    """评审实录回归：提到“酒店”的时间调整走行程编辑，不被关键词劫持进候选流。"""
    request = _request("把酒店挪到晚上，博物馆放上午")
    _stub_decision(
        monkeypatch,
        {
            "mode": "plan_update",
            "reply": "已把入住调到晚上、上午留给景点",
            "patches": [
                {"op": "update", "item_id": 2, "fields": {"start_time": "21:00"}},
                {"op": "update", "item_id": 1, "fields": {"start_time": "09:00"}},
            ],
            "operations": [],
        },
    )
    response = decide._chat_turn_response(request)
    assert response.changed and response.plans, "时间调整必须产出可应用草稿"
    hotel = next(item for item in response.plans[0]["items"] if item["id"] == 2)
    assert hotel["start_time"] == "21:00"
    assert not response.hotel_options and response.pending_action is None


def test_explicit_hotel_change_still_routed_to_candidates(monkeypatch):
    """守卫不得误伤真换酒店：模型误判成 plan_update 时仍进候选流程。"""
    request = _request("帮我换一个酒店")
    _stub_decision(monkeypatch, {"mode": "plan_update", "reply": "", "patches": [], "operations": []})
    response = decide._chat_turn_response(request)
    assert response.pending_action is not None
    assert response.pending_action.get("type") == "replace_hotel"


# ---------- 残留①：基线遗留时间重叠的预检与放行（2026-10-06） ----------


def _conflicted_request(message: str) -> ChatTurnRequest:
    """基线 day1 自带重叠（终检放行的存量行程形态）的请求夹具。"""
    request = _request(message)
    request.plans[0]["items"].append(
        {
            "id": 9,
            "item_type": "attraction",
            "poi_name": "断桥残雪",
            "start_time": "09:00",  # 与苏堤春晓 08:30-10:00 重叠
            "end_time": "10:30",
            "duration_min": 90,
        }
    )
    return request


def test_inherited_conflict_no_longer_blocks_draft(monkeypatch):
    """基线遗留重叠 + 编辑没引入新重叠 → 放行出草稿，回复注明遗留问题。"""
    request = _conflicted_request("把灵隐寺改到下午三点")
    _stub_decision(
        monkeypatch,
        {
            "mode": "plan_update",
            "reply": "已把灵隐寺挪到下午",
            "patches": [{"op": "update", "item_id": 3, "fields": {"start_time": "15:00"}}],
            "operations": [],
        },
    )
    response = decide._chat_turn_response(request)
    assert response.changed and response.plans, "继承冲突不得拦下与它无关的编辑"
    assert "遗留问题" in response.reply, "放行时要如实注明，不假装计划完美"
    assert "苏堤春晓" in response.reply and "断桥残雪" in response.reply


def test_newly_introduced_conflict_is_still_rejected(monkeypatch):
    """编辑新引入的重叠仍硬拒绝：放行只针对基线已有的冲突对。"""
    request = _request("把入住改到上午八点四十五")
    _stub_decision(
        monkeypatch,
        {
            "mode": "plan_update",
            "reply": "已把入住挪到上午",
            "patches": [{"op": "update", "item_id": 2, "fields": {"start_time": "08:45"}}],
            "operations": [],
        },
    )
    response = decide._chat_turn_response(request)
    assert not response.changed and not response.plans
    assert "时间冲突" in response.reply


def test_baseline_conflict_surfaced_in_decide_prompt(monkeypatch):
    """预检提示：基线带重叠时把冲突点写进 decide 输入，模型有机会顺手修复。"""
    request = _conflicted_request("随便调整一下")

    class _CapturingClient:
        def chat(self, messages, **kwargs):
            self.user_content = messages[-1]["content"]
            return json.dumps({"mode": "no_change", "reply": "无需修改", "operations": []})

    client = _CapturingClient()
    monkeypatch.setattr(decide, "get_llm_client", lambda: client)
    monkeypatch.setattr(decide.tools, "search_hotels", lambda *args, **kwargs: [])
    decide._chat_turn_response(request)
    assert "已知遗留问题" in client.user_content
    assert "苏堤春晓" in client.user_content and "断桥残雪" in client.user_content


def test_pure_deletion_revealing_new_pair_of_occupied_hotel_passes(monkeypatch):
    """活体 R4 实录（2026-10-06 残留①复验）：基线全占酒店 07:00-23:59 与甲乙都叠，
    pairwise 相邻配对只产生 (酒店,甲)；模型删掉甲后 (酒店,乙) 成为新相邻对——
    三元组精确匹配会误判 novel，按天继承（纯删除、零时间改动）必须放行。"""
    request = _request("把秋叶原和银座的购物都去掉")
    request.plans[0]["items"].insert(
        0,
        {
            "id": 8,
            "item_type": "hotel",
            "poi_name": "东京站酒店",
            "start_time": "07:00",
            "end_time": "23:59",
            "duration_min": 1019,
        },
    )
    decision = {
        "mode": "plan_update",
        "reply": "已去掉购物点",
        "patches": [
            {"op": "delete", "item_id": 1},  # 苏堤春晓 08:30-10:00（与全占酒店叠加）
        ],
        "operations": [],
    }
    _stub_decision(monkeypatch, decision)
    response = decide._chat_turn_response(request)
    assert response.changed and response.plans, "纯删除不得被全占酒店的新相邻对拒绝"
    assert "遗留问题" in response.reply


def test_time_change_on_conflicted_day_still_rejected(monkeypatch):
    """按天继承只保护纯删除：该天改过时间又出现基线没有的冲突对 → 仍拒绝。"""
    request = _conflicted_request("把入住改到上午八点四十五")
    _stub_decision(
        monkeypatch,
        {
            "mode": "plan_update",
            "reply": "已挪",
            "patches": [{"op": "update", "item_id": 2, "fields": {"start_time": "08:45"}}],
            "operations": [],
        },
    )
    response = decide._chat_turn_response(request)
    assert not response.changed and not response.plans
    assert "时间冲突" in response.reply


def test_unrepairable_plan_edit_degrades_with_forensics_log(monkeypatch, caplog):
    """主提案与反馈修复轮都产出不可落地补丁、且非精简/延长类（无确定性兜底）时：
    静默降级并留排障日志（2026-10-06 R1 取证的锚点行，夜审按它定位死因）。"""
    request = _request("随便调整一下")

    def _garbage(req, hotels, feedback=None):
        return {
            "mode": "rewrite_plan",
            "reply": "",
            # days 数与目标天数不符（夹具 2 天只给 1 天且 day_no 集合错）→ 结构校验必拒
            "plan_document": {
                "schema_version": 1,
                "trip": {
                    "city": "杭州",
                    "days": 2,
                    "persons": 2,
                    "budget": None,
                    "start_date": None,
                    "end_date": None,
                    "preferences": [],
                    "hotel_tier": None,
                },
                "days": [{"day_no": 3, "items": []}],
            },
            "operations": [],
        }

    monkeypatch.setattr(decide, "_decide_plan_change", _garbage)
    monkeypatch.setattr(decide.tools, "search_hotels", lambda *args, **kwargs: [])
    with caplog.at_level("WARNING", logger="app.agent.editing.chat_draft.decide"):
        response = decide._chat_turn_response(request)
    assert not response.changed and not response.plans
    assert "无法生成安全草稿" in response.reply
    assert any("plan edit rejected after repair" in record.message for record in caplog.records)
    assert any("repair_produced=invalid" in record.message for record in caplog.records)
