"""评测防倒退门禁的单测（SPEC v2.3 §8 A2 / §12 / C3.2 深度指标 / P2-6 分路径与指纹）。

`check()` 是纯函数：这里逐类钉住通过线与红灯（failed / 版本或生成路径漂移 / 一致率
倒退 / 深度指标倒退 / 字段缺失 / Prompt 正文指纹漂移 / 分路径期望），并显式钉住
「degraded 不判失败」这条纪律——它是防口径造假的护栏。`main()` 只测 guard 分支
（无 key 跳过 / 报告缺失 / 报告路径与期望路径错配），不触网。
"""

from __future__ import annotations

from tests.agent_eval import eval_gate
from tests.agent_eval.prompt_digest import prompt_text_sha256


def _report(**overrides) -> dict:
    base = {
        "mode": "real-llm",
        "generation_path": "stream",
        "prompt_version": eval_gate.EXPECTED_PROMPT_VERSION,
        "open_day_prompt_version": eval_gate.EXPECTED_OPEN_DAY_PROMPT_VERSION,
        "open_trip_prompt_version": eval_gate.EXPECTED_OPEN_TRIP_PROMPT_VERSION,
        "prompt_sha256": prompt_text_sha256(),
        "case_count": 3,
        "run_count": 6,
        "status_counts": {"success": 0, "degraded": 6, "failed": 0},
        "consistency_rate": 0.1667,
        "depth_metrics": {
            "coord_valid_rate": 1.0,
            "deeplink_resolvable_rate": 1.0,
            "category_reasonable_rate": 1.0,
        },
    }
    base.update(overrides)
    return base


def test_gate_passes_on_baseline_report() -> None:
    assert eval_gate.check(_report()) == []


def test_degraded_is_never_treated_as_failure() -> None:
    """degraded 是本项目如实呈现的状态；判失败会逼出口径造假（A2 纪律）。"""
    report = _report(status_counts={"success": 0, "degraded": 12, "failed": 0})
    assert eval_gate.check(report) == []


def test_failed_run_is_hard_failure() -> None:
    problems = eval_gate.check(_report(status_counts={"success": 10, "degraded": 1, "failed": 1}))
    assert any("failed" in problem for problem in problems)


def test_prompt_version_drift_is_flagged() -> None:
    problems = eval_gate.check(_report(open_day_prompt_version="v9.9.next"))
    assert any("open_day_prompt_version" in problem for problem in problems)

    problems = eval_gate.check(_report(prompt_version="workflow-v3"))
    assert any("prompt_version" in problem for problem in problems)


def test_generation_path_drift_is_flagged() -> None:
    """换被测路径 = 换基线：同步图与逐日流式的预算形状不同，数值不许互相顶替。

    旧报告（没有 generation_path 键）也判红——那正是 2026-08-29 那份 run 的形状，
    它测的是 graph 路径，不能拿来给 stream 门禁背书。
    """
    problems = eval_gate.check(_report(generation_path="graph"))
    assert any("generation_path" in problem for problem in problems)

    legacy = _report()
    legacy.pop("generation_path")
    assert any("generation_path" in problem for problem in eval_gate.check(legacy))


def test_consistency_regression_is_flagged(monkeypatch) -> None:
    """低于一致性下限判红。下限现值 0.0（2026-09-23 流式小样本实测零命中，防倒退语义
    下 0 不可再跌），所以这里把下限抬到 0.5 验证绊线本身——棘轮随稳定性改进抬升后，
    这条自动获得真实分辨力。

    打补丁的位置是 `PATH_EXPECTATIONS` 里的期望（P2-6 之后按路径分列，模块级常量
    只是 stream 那份的初始值）。
    """
    monkeypatch.setitem(eval_gate.PATH_EXPECTATIONS["stream"], "consistency_baseline", 0.5)
    problems = eval_gate.check(_report(consistency_rate=0.4))
    assert any("一致率" in problem for problem in problems)


def test_missing_consistency_rate_is_flagged() -> None:
    report = _report()
    report.pop("consistency_rate")
    assert eval_gate.check(report)


def test_prompt_text_digest_drift_is_flagged(monkeypatch) -> None:
    """P2-6：版本号是手抄常量，改 Prompt 正文而不 bump 时只有指纹能拦（消灭荣誉制）。"""
    problems = eval_gate.check(_report(prompt_sha256="0" * 64))
    assert any("Prompt 正文指纹" in problem for problem in problems)

    legacy = _report()
    legacy.pop("prompt_sha256")
    assert any("prompt_sha256 缺失" in problem for problem in eval_gate.check(legacy))

    # 反证：指纹与源码现值一致时这条不红（避免门禁"永远红"的假防线）
    assert eval_gate.check(_report()) == []


def test_graph_path_expectations_are_separate() -> None:
    """P2-6：trip 图路径的报告必须拿 graph 期望查，不能借 stream 的深度基线背书。"""
    graph_report = _report(
        generation_path="graph",
        # category_reasonable_rate 是 stream 路径唯一仍活跃的深度下限（1.0）：用它
        # 验证「同一份低值报告在 graph 过、在 stream 红」——coord/deeplink 已按
        # 2026-10-08 登记降为 presence-only，两边都是 0，分不出路径差异。
        depth_metrics={"coord_valid_rate": 0.1, "deeplink_resolvable_rate": 0.0, "category_reasonable_rate": 0.0},
    )
    assert eval_gate.check(graph_report, expected_path="graph") == []
    # 同一份报告拿去当 stream 报告查 → 路径漂移 + 深度低于 stream 基线，两条都要红
    problems = eval_gate.check(graph_report, expected_path="stream")
    assert any("generation_path" in problem for problem in problems)
    assert any("category_reasonable_rate" in problem for problem in problems)
    # 未知路径不静默通过
    assert eval_gate.check(graph_report, expected_path="trip")


def test_resolve_picks_expectations_by_flag_and_filename() -> None:
    """报告路径与期望路径不许错配：--path graph 与 *_graph.json 都指向 graph 期望。"""
    path, expected = eval_gate._resolve(["eval_gate", "--path", "graph"])
    assert expected == "graph" and path == eval_gate.GRAPH_REPORT
    path, expected = eval_gate._resolve(["eval_gate", "some/llm_report_graph.json"])
    assert expected == "graph"
    path, expected = eval_gate._resolve(["eval_gate"])
    assert expected == "stream" and path == eval_gate.DEFAULT_REPORT
    path, expected = eval_gate._resolve(["eval_gate", "some/llm_report.json"])
    assert expected == "stream" and path is not None and path.name == "llm_report.json"


def test_depth_metric_regression_is_flagged(monkeypatch) -> None:
    """C3.2：深度指标低于记录基线判红（防倒退，非达标线）。

    2026-10-08 登记后 coord/deeplink 的 stream 下限是 presence-only（0.0，波动由
    外部地理编码可用性主导，见 eval_gate 模块注与 AGENTS.md 登记项）：现值多低都
    不再判红，这道绊线改用抬升后的期望验证机制本身（与上面一致率绊线同一打法）。
    category_reasonable_rate（1.0，生成质量属性）是仍活跃的下限，直接用现值验证。
    """
    # 登记后的现行为（未抬期望）：coord/deeplink 任意低值不红（presence-only），
    # category 仍活跃——先验这段，再抬期望验绊线（setitem 会污染后续断言）。
    live = _report(
        depth_metrics={"coord_valid_rate": 0.0, "deeplink_resolvable_rate": 0.0, "category_reasonable_rate": 1.0}
    )
    assert eval_gate.check(live) == []
    assert any(
        "category_reasonable_rate" in problem
        for problem in eval_gate.check(
            _report(
                depth_metrics={
                    "coord_valid_rate": 1.0,
                    "deeplink_resolvable_rate": 1.0,
                    "category_reasonable_rate": 0.9,
                }
            )
        )
    )

    monkeypatch.setitem(
        eval_gate.PATH_EXPECTATIONS["stream"],
        "depth",
        {"coord_valid_rate": 0.8, "deeplink_resolvable_rate": 0.3, "category_reasonable_rate": 1.0},
    )
    problems = eval_gate.check(
        _report(
            depth_metrics={"coord_valid_rate": 0.5, "deeplink_resolvable_rate": 1.0, "category_reasonable_rate": 1.0}
        )
    )
    assert any("coord_valid_rate" in problem for problem in problems)


def test_missing_depth_metrics_is_flagged() -> None:
    """报告缺 depth_metrics（旧口径产物）→ 判红，提示先重跑 llm_eval。"""
    report = _report()
    report.pop("depth_metrics")
    problems = eval_gate.check(report)
    assert any("depth_metrics" in problem for problem in problems)


def test_main_skips_without_llm_key(monkeypatch, capsys) -> None:
    monkeypatch.setattr(eval_gate.settings, "llm_api_key", "")
    assert eval_gate.main(["eval_gate"]) == 0
    assert "跳过" in capsys.readouterr().out


def test_main_fails_when_report_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(eval_gate.settings, "llm_api_key", "example-key")
    assert eval_gate.main(["eval_gate", str(tmp_path / "nope.json")]) == 1


def test_main_reads_report_and_reports_problems(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(eval_gate.settings, "llm_api_key", "example-key")
    path = tmp_path / "llm_report.json"
    path.write_text('{"mode": "real-llm"}', encoding="utf-8")
    assert eval_gate.main(["eval_gate", str(path)]) == 1
    assert "未通过" in capsys.readouterr().out
