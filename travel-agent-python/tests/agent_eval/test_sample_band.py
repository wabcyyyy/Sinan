"""sample_band 的口径测试：全量闸门、run1 聚合、generation_path 闸门、重复采样拒绝、LF 字节形态。"""

import pytest

from tests.agent_eval.sample_band import append_sample, build_row

# 夹具是 2 例小样本：显式告诉 build_row 期望值，绕开"标准数据集全量"闸门
_EXPECT = 2


def _row(report) -> dict:
    return build_row(report, expected_cases=_EXPECT)


def _report(**over):
    base = {
        "generated_at": "2026-10-08T00:00:00+00:00",
        "prompt_version": "p1",
        "generation_path": "stream",
        "case_count": 2,
        "depth_metrics": {
            "coord_valid_rate": 0.8,
            "deeplink_resolvable_rate": 0.5,
            "category_reasonable_rate": 1.0,
        },
        "details": [
            {"run1": {"quality": {"terminal_reset_day_rate": 0.5, "terminal_reset_reasons": {"行程过满": 1}}}},
            {
                "run1": {
                    "quality": {"terminal_reset_day_rate": 1.0, "terminal_reset_reasons": {"行程过满": 2, "无餐饮": 1}}
                }
            },
        ],
    }
    base.update(over)
    return base


def test_build_row_uses_run1_aggregates_and_report_depth():
    row = _row(_report())
    assert row["terminal_reset_day_rate"] == 0.75
    assert row["terminal_reset_reasons"] == {"无餐饮": 1, "行程过满": 3}
    assert row["coord_valid_rate"] == 0.8
    assert row["generation_path"] == "stream"


def test_build_row_treats_failed_run1_as_zero_like_depth_metrics():
    report = _report(details=[_report()["details"][0], {"run1": None}])
    assert _row(report)["terminal_reset_day_rate"] == 0.25


def test_build_row_refuses_partial_run_missing_path_or_details():
    with pytest.raises(ValueError, match="全量"):
        build_row(_report())  # 夹具 case_count=2 ≠ 标准数据集（cases.json 的真实用例数）
    with pytest.raises(ValueError, match="generation_path"):
        _row(_report(generation_path=""))
    with pytest.raises(ValueError, match="details"):
        _row(_report(details=[]))


def test_append_sample_dedupes_by_time_and_path_writes_lf(tmp_path):
    samples = tmp_path / "band_samples.jsonl"
    row = _row(_report())
    assert append_sample(samples, row) is True
    assert append_sample(samples, row) is False, "同 generated_at + generation_path 不得重复入带"
    # 同一 run 换路径是另一行（口径不可混用 ≠ 不许都采）
    assert append_sample(tmp_path / "b.jsonl", _row(_report(generation_path="graph"))) is True
    raw = samples.read_bytes()
    assert raw.endswith(b"\n") and b"\r\n" not in raw
    assert len(samples.read_text(encoding="utf-8").strip().splitlines()) == 1
