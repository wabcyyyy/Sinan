"""planNote/tripTheme 与实际住宿条目的一致性校验（2026-09-30 评审遗留项）。

评审实录：行程 #7 planNote 写「博舍酒店为据点」，实际条目是「成都首座万豪酒店」。
修复后：不一致以 NARRATIVE_HOTEL_MISMATCH 质量警告呈现，qualityStatus 降为
READY_WITH_WARNINGS（与待复核事实同级）；泛指提及不误报。
"""

from __future__ import annotations

from app.db.models import ItineraryItem
from app.services.itinerary_query import (
    QUALITY_RULE_VERSION,
    _narrative_hotel_mismatches,
    _quality_issues,
    _quality_status,
)


def _hotel(name: str) -> ItineraryItem:
    return ItineraryItem(item_type="hotel", poi_name=name)


def _attraction(name: str) -> ItineraryItem:
    return ItineraryItem(item_type="attraction", poi_name=name)


def test_reviewer_case_boshe_vs_marriott_flags_mismatch():
    items = [_hotel("成都首座万豪酒店"), _attraction("宽窄巷子")]
    assert _narrative_hotel_mismatches("以博舍酒店为据点展开", None, items) == ["博舍酒店"]


def test_matching_mention_passes():
    items = [_hotel("成都首座万豪酒店")]
    assert _narrative_hotel_mismatches("以成都首座万豪酒店为据点展开", None, items) == []
    assert _narrative_hotel_mismatches("酒店选在万豪附近，地铁直达", None, items) == []


def test_generic_mentions_do_not_misfire():
    items = [_hotel("成都首座万豪酒店")]
    assert _narrative_hotel_mismatches("回酒店休息，住的地方离地铁近", None, items) == []
    assert _narrative_hotel_mismatches("把酒店挪到晚上再出门", None, items) == []


def test_no_hotel_items_skips_check():
    assert _narrative_hotel_mismatches("以博舍酒店为据点", None, [_attraction("宽窄巷子")]) == []


def test_prefix_modifier_still_matches_actual_hotel():
    items = [_hotel("成都首座万豪酒店")]
    assert _narrative_hotel_mismatches("旁边的成都首座万豪酒店就是大本营", None, items) == []


def test_trip_theme_also_checked():
    items = [_hotel("杭州西子宾馆汪庄")]
    assert _narrative_hotel_mismatches(None, "以四季酒店为基点游西湖", items) == ["四季酒店"]


def test_mismatch_degrades_quality_status_and_warning():
    items = [_hotel("成都首座万豪酒店")]
    assert _quality_status(2, items, [], 0, ["博舍酒店"]) == "READY_WITH_WARNINGS"
    issues = _quality_issues("READY_WITH_WARNINGS", [], items, 0, ["博舍酒店"])
    assert any(issue["code"] == "NARRATIVE_HOTEL_MISMATCH" for issue in issues)
    assert any("博舍酒店" in issue["message"] for issue in issues)


def test_pending_facts_and_mismatch_both_reported():
    items = [_hotel("成都首座万豪酒店")]
    issues = _quality_issues("READY_WITH_WARNINGS", [], items, 2, ["博舍酒店"])
    codes = {issue["code"] for issue in issues}
    assert {"NARRATIVE_HOTEL_MISMATCH", "FACT_REQUIRES_REVIEW"} <= codes


def test_clean_ready_trip_unaffected():
    items = [_hotel("成都首座万豪酒店")]
    assert _quality_status(2, items, [], 0, []) == "READY"
    assert _quality_issues("READY", [], items, 0, []) == []


def test_rule_version_bumped():
    assert QUALITY_RULE_VERSION == "travel-quality-1.1"
