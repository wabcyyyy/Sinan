"""stream_events 契约测试：事件 wire 形状 + schema 快照 + 必填字段钉死。

这些断言是跨语言契约的「Python 侧闸门」：
- 形状测试防止产出点手搓键名回潮；
- 快照测试保证入仓的 contracts/stream_events.schema.json 与模型同步
  （CI 另有 git diff 漂移门禁，此处双保险并给出可读失败信息）；
- 必填字段钉死防止「字段被改可选」这类静默弱化。
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.stream_events import (
    DayEvent,
    DayPatchEvent,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    SuggestionsEvent,
    export_schema,
    to_wire,
)
from app.schemas.trip import DailyPlan, Suggestion, TripItem

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "stream_events.schema.json"


class TestEventWireShape:
    def test_start_and_error_wire_keys(self):
        assert to_wire(StartEvent(type="start", run_id="run-1")) == {"type": "start", "runId": "run-1"}
        assert to_wire(ErrorEvent(type="error", message="boom")) == {"type": "error", "message": "boom"}

    def test_day_event_plan_wire_shape(self):
        plan = DailyPlan(day_no=2, items=[TripItem(poi_name="圣家堂", item_type="attraction")])
        event = to_wire(DayEvent(type="day", plan=plan))
        assert event["type"] == "day"
        assert event["plan"]["dayNo"] == 2
        assert event["plan"]["items"][0]["poiName"] == "圣家堂"

    def test_day_patch_is_distinct_type(self):
        event = to_wire(DayPatchEvent(type="day_patch", plan=DailyPlan(day_no=1)))
        assert event["type"] == "day_patch"
        assert event["plan"]["dayNo"] == 1

    def test_suggestions_event_items(self):
        event = to_wire(SuggestionsEvent(type="suggestions", items=[Suggestion(name="米拉之家")]))
        assert event["type"] == "suggestions"
        assert event["items"][0]["name"] == "米拉之家"
        assert event["items"][0]["needReservation"] is False

    def test_done_event_wire_keys(self):
        event = to_wire(
            DoneEvent(
                type="done",
                days_expected=3,
                days_emitted=[1, 2],
                trip_theme=None,
                complete=False,
                message="upstream down",
            )
        )
        assert event == {
            "type": "done",
            "daysExpected": 3,
            "daysEmitted": [1, 2],
            "tripTheme": None,
            "complete": False,
            "message": "upstream down",
        }

    def test_type_literal_mismatch_rejected(self):
        with pytest.raises(ValidationError):
            # dict-splat 绕过类型层的 Literal 检查，运行时仍由 pydantic 拒绝（负例意图不变）
            DayEvent(**{"type": "day_patch", "plan": DailyPlan(day_no=1)})

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            # 同上：运行时仍缺必填字段，ValidationError 意图不变
            DoneEvent(**{"type": "done", "days_expected": 1, "complete": True})  # 缺 days_emitted 等


class TestSchemaContract:
    def test_committed_schema_matches_export(self):
        committed = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert committed == export_schema(), (
            "contracts/stream_events.schema.json 与模型不同步："
            "运行 uv run python scripts/export_contracts.py 重新导出并提交"
        )

    def test_six_event_types_declared(self):
        schema = export_schema()
        assert set(schema["discriminator"]["mapping"]) == {"start", "day", "day_patch", "suggestions", "done", "error"}

    def test_required_fields_pinned(self):
        """Java 消费方依赖的字段必须保持必填（改名/降级可选都会在此暴露）。"""
        defs = export_schema()["$defs"]
        assert defs["DayEvent"]["required"] == ["type", "plan"]
        assert set(defs["DoneEvent"]["required"]) == {
            "type",
            "daysExpected",
            "daysEmitted",
            "tripTheme",
            "complete",
            "message",
        }
        for model in ("DailyPlan", "TripItem", "Suggestion", "DayOption", "FactEvidence"):
            props = defs[model]["properties"]
            assert defs[model]["required"] == list(props), f"{model} 应全键必填"

    def test_all_object_schemas_require_every_property(self):
        """wire 契约不变式：dump 必带全部键（可空值为 null），故 required == properties。"""

        def check(node):
            if isinstance(node, dict):
                props = node.get("properties")
                if isinstance(props, dict) and props:
                    assert node.get("required") == list(props)
                for key, value in node.items():
                    if key in {"properties", "patternProperties", "$defs"} and isinstance(value, dict):
                        for sub in value.values():
                            check(sub)
                    else:
                        check(value)
            elif isinstance(node, list):
                for item in node:
                    check(item)

        check(export_schema())

    def test_descriptions_stripped(self):
        """docstring 只服务 Python 可读性，不应进入契约（避免文字改动触发漂移）。"""
        assert "description" not in json.dumps(export_schema(), ensure_ascii=False)
