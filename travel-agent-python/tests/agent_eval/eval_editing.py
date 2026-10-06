"""editing 域离线评测（PR-8）：nl_edit / clarify / city_guide / local_replan 四入口。

四个 editing 入口此前零评测。本文件用 `mock_llm` 的编辑域替身（LLM 出口替身）+
确定性用例跑出八个指标，落 `report/offline/editing_report.json|md`，指标进
`metrics_baseline.json` 棘轮（eval_ratchet 数据驱动）；报告记 cassette 哈希
（PR-8 风险栏：cassette 资产可追溯）。local_replan 无 LLM（纯确定性换排）直接跑，
用例形状与 tests/test_local_replan.py 同源。

用法：`uv run python tests/agent_eval/eval_editing.py`
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.editing import city_guide as city_guide_mod
from app.agent.editing import clarify as clarify_mod
from app.agent.editing import local_replan as local_replan_mod
from app.agent.editing import nl_edit as nl_edit_mod
from app.agent.tools import impl as tools
from app.common.config import settings
from app.schemas.trip import ClarifyRequest, EditOpRequest, LocalReplanRequest
from tests.agent_eval import mock_llm
from tests.agent_eval.cassette import cassette_sha256

REPORT_DIR = Path(__file__).parent / "report" / "offline"
CASSETTE_DIR = Path(__file__).parent / "cassettes"

_ALLOWED_ACTIONS = {"delete", "add", "move_day", "update_time", "upgrade_hotel"}


class _FakeClient:
    """editing 流程的 LLM 出口替身：回固定 raw JSON（mock_llm 的编辑域 fixture）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return self.reply

    def chat(self, *args: Any, **kwargs: Any) -> str:
        return self.reply


def _plans() -> list[dict]:
    return [
        {
            "day_no": 1,
            "items": [{"item_type": "attraction", "poi_name": "西湖"}, {"item_type": "food", "poi_name": "楼外楼"}],
        },
        {"day_no": 2, "items": [{"item_type": "attraction", "poi_name": "灵隐寺"}]},
    ]


def _case_nl_edit(reply: str, instruction: str, plans: list[dict], expected_hits: int) -> dict:
    with _patch(nl_edit_mod, reply):
        ops = nl_edit_mod.run_edit_ops(EditOpRequest(city="杭州", days=2, plans=plans, instruction=instruction))
    names = {str(item.get("poi_name")) for plan in plans for item in plan.get("items") or []}
    valid = bool(ops) and len(ops) <= 8 and all(op.action in _ALLOWED_ACTIONS for op in ops)
    hits = sum(1 for op in ops if op.poi_name and op.poi_name in names)
    return {
        "valid": valid,
        "target_hit": hits >= expected_hits,
        "returned": len(ops),
    }


class _patch:
    """把 editing 模块的 get_llm_client 换成固定回复替身（用完即还原）。"""

    def __init__(self, module, reply: str) -> None:
        self.module = module
        self.reply = reply
        self._original = None

    def __enter__(self):
        self._original = self.module.get_llm_client
        self.module.get_llm_client = lambda: _FakeClient(self.reply)
        return self

    def __exit__(self, *exc) -> None:
        self.module.get_llm_client = self._original


def _forbid_network_call(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("离线评测不得发起实时价/联网查询")


def _offline_guard(stack: ExitStack) -> None:
    """离线护栏（eval_research 同款姿势 + eval_agent 的"证明离线"哨兵）：local_replan
    的缺名候选解析走注册表 `search_pois`（组合 tools.search_attractions/search_foods，
    调用期解析）→ places → 真库 get_city_geo / web_search / LLM。换 mock_llm 确定性
    替身后报告才不随环境漂移，eval-determinism 的哈希才有意义。
    """
    stack.enter_context(patch.object(tools, "search_attractions", mock_llm.search_attractions))
    stack.enter_context(patch.object(tools, "search_foods", mock_llm.search_foods))
    stack.enter_context(patch.object(tools, "search_hotels", mock_llm.search_hotels))
    stack.enter_context(patch.object(settings, "web_search_enabled", False))
    stack.enter_context(patch.object(settings, "nominatim_enabled", False))
    stack.enter_context(patch("app.agent.generation.output.prices.query_live_price", _forbid_network_call))
    stack.enter_context(patch("app.agent.generation.output.prices.query_live_food_price", _forbid_network_call))
    # LA2 酒店实时价（google_hotels）同为外网入口，一律钉死
    stack.enter_context(patch("app.agent.data.live_quotes.fetch_hotel_live_quotes", _forbid_network_call))


def _case_clarify(reply: str, message: str, slots: dict, expected_filled: list[str], expect_question: bool) -> dict:
    with _patch(clarify_mod, reply):
        response = clarify_mod.run_clarify(ClarifyRequest(message=message, slots=slots))
    filled = sum(1 for key in expected_filled if response.slots.get(key) not in (None, "", "null"))
    question_ok = bool(response.question) if expect_question else response.question is None
    return {
        "slot_filled": filled,
        "slot_expected": len(expected_filled),
        "question_ok": question_ok,
        "ready_ok": response.ready == (not response.missing),
    }


def _case_city_guide(reply: str, user_input: str, expected_kind: str) -> dict:
    with _patch(city_guide_mod, reply):
        result = city_guide_mod.run_city_guide({"input": user_input, "history": []})
    suggestions = result.get("suggestions") or []
    shape_ok = all(str(s.get("name") or "").strip() and str(s.get("reason") or "").strip() for s in suggestions)
    return {
        "kind_ok": result.get("kind") == expected_kind and result.get("kind") in ("province", "city", "unclear"),
        "suggestion_shape_ok": shape_ok and len(suggestions) <= 2,
    }


def _replan_item(name: str, start: str = "09:00", end: str = "10:00") -> dict:
    return {
        "item_type": "attraction",
        "poi_id": name,
        "poi_name": name,
        "latitude": 30.0,
        "longitude": 120.0,
        "start_time": start,
        "end_time": end,
        "duration_min": 60,
    }


def _case_replan_replace() -> dict:
    plans = [
        {"day_no": 1, "items": [_replan_item("Day1")]},
        {"day_no": 2, "items": [_replan_item("Locked"), _replan_item("Old", start="11:00", end="12:00")]},
    ]
    result = local_replan_mod.run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[2],
            locked_names=["Locked"],
            candidate_names=["Day1"],
            plans=plans,
            action_id="eval-replan-1",
        )
    )
    kept_locked = any(item.get("poi_name") == "Locked" for item in result["plans"][1]["items"])
    replaced = any(row.get("from") == "Old" for row in result.get("replaced_items") or [])
    return {
        "lock_respected": kept_locked,
        "replaced": replaced,
        "expects_replacement": True,
        "status_ok": result["status"] in ("success", "degraded"),
    }


def _case_replan_unknown_candidate() -> dict:
    result = local_replan_mod.run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[1],
            candidate_names=["不存在的点位"],
            plans=[{"day_no": 1, "items": [_replan_item("A")]}],
        )
    )
    return {"status_ok": result["status"] == "failed", "replaced": False}


def build_report() -> dict:
    plans = _plans()
    edit_cases = [
        _case_nl_edit(
            mock_llm.fixture_edit_reply([{"action": "delete", "day_no": 2, "poi_name": "灵隐寺"}]),
            "删掉第 2 天的灵隐寺",
            plans,
            expected_hits=1,
        ),
        _case_nl_edit(
            mock_llm.fixture_edit_reply(
                [{"action": "update_time", "day_no": 1, "poi_name": "西湖", "start_time": "10:30"}]
            ),
            "把第 1 天的西湖改到 10:30 出发",
            plans,
            expected_hits=1,
        ),
        _case_nl_edit(
            mock_llm.fixture_edit_reply(
                [
                    {"action": "destroy", "day_no": 1},
                    *[{"action": "delete", "day_no": 1, "poi_name": "西湖"} for _ in range(9)],
                ]
            ),
            "乱操作压测：非法 action 与超量输出",
            plans,
            expected_hits=1,
        ),
    ]
    clarify_cases = [
        _case_clarify(
            mock_llm.fixture_clarify_reply(city="杭州", days=3),
            "想去杭州玩 3 天",
            {},
            expected_filled=["city", "days"],
            expect_question=True,
        ),
        _case_clarify(
            mock_llm.fixture_clarify_reply(
                city="杭州", days=2, persons=2, budget=3000, hotel_tier="舒适", preferences=["美食"], stay_nights=1
            ),
            "杭州两天两个人预算三千住舒适型爱吃",
            {},
            expected_filled=["city", "days", "persons", "budget", "hotel_tier", "stay_nights"],
            expect_question=False,
        ),
    ]
    guide_cases = [
        _case_city_guide(
            mock_llm.fixture_city_guide_reply(
                "province",
                "云南",
                "云南是个好去处，昆明的四季如春、大理的风花雪月、丽江的古城区都值得走走。",
                [{"name": "昆明", "reason": "四季如春，中转方便"}],
            ),
            "我想去云南玩",
            # 流程契约归一：有具体落点的 province 判定收敛为 city（city_guide 尾段）
            "city",
        ),
        _case_city_guide(
            mock_llm.fixture_city_guide_reply("unclear", None, "想去哪里？说说你的想法，TA 来帮你参谋。", []),
            "随便看看",
            "unclear",
        ),
    ]
    with ExitStack() as stack:
        _offline_guard(stack)
        replan_cases = [_case_replan_replace(), _case_replan_unknown_candidate()]
    edit_valid = sum(1 for case in edit_cases if case["valid"]) / len(edit_cases)
    edit_hits = sum(1 for case in edit_cases if case["target_hit"]) / len(edit_cases)
    slot_filled = sum(case["slot_filled"] for case in clarify_cases)
    slot_expected = sum(case["slot_expected"] for case in clarify_cases)
    question_ok = sum(1 for case in clarify_cases if case["question_ok"]) / len(clarify_cases)
    kind_ok = sum(1 for case in guide_cases if case["kind_ok"]) / len(guide_cases)
    shape_ok = sum(1 for case in guide_cases if case["suggestion_shape_ok"]) / len(guide_cases)
    lock_cases = [case for case in replan_cases if "lock_respected" in case]
    lock_ok = sum(1 for case in lock_cases if case["lock_respected"]) / max(len(lock_cases), 1)
    replace_cases = [case for case in replan_cases if case.get("expects_replacement")]
    replaced_rate = sum(1 for case in replace_cases if case["replaced"]) / max(len(replace_cases), 1)

    cassettes = {}
    for path in sorted(CASSETTE_DIR.glob("*.json")):
        cassettes[path.name] = cassette_sha256(path)

    return {
        "mode": "offline-editing",
        "case_count": len(edit_cases) + len(clarify_cases) + len(guide_cases) + len(replan_cases),
        "cassettes": cassettes,
        "metrics": {
            "edit_ops_valid_rate": round(edit_valid, 4),
            "edit_target_hit_rate": round(edit_hits, 4),
            "clarify_slot_fill_rate": round(slot_filled / max(slot_expected, 1), 4),
            "clarify_question_valid_rate": round(question_ok, 4),
            "guide_kind_valid_rate": round(kind_ok, 4),
            "guide_suggestion_shape_rate": round(shape_ok, 4),
            "replan_lock_respected_rate": round(lock_ok, 4),
            "replan_replaced_rate": round(replaced_rate, 4),
        },
        "details": {
            "nl_edit": edit_cases,
            "clarify": clarify_cases,
            "city_guide": guide_cases,
            "local_replan": replan_cases,
        },
    }


def write_report(report: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "editing_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metrics = report["metrics"]
    lines = [
        "# editing 域离线评测报告",
        "",
        f"- 用例数：{report['case_count']}（nl_edit / clarify / city_guide / local_replan）",
    ]
    for name, digest in (report.get("cassettes") or {}).items():
        lines.append(f"- cassette：`{name}`（sha256 {digest[:12]}…）")
    lines += ["", "| 指标 | 结果 |", "| --- | ---: |"]
    for key, value in metrics.items():
        lines.append(f"| {key} | {value:.2%} |")
    (REPORT_DIR / "editing_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    report = build_report()
    write_report(report)
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print(f"报告已生成：{REPORT_DIR / 'editing_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
