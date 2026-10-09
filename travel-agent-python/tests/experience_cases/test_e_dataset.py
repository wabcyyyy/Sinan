"""固定体验回归题集的 pytest 集成（M7，spec 2026-10-08 §13.2）。

`pytest tests/experience_cases -q` 一键跑：schema/hash 漂移检测 + 全量 check 执行
（零 fail 才绿；skip 会在报告中列明）。与 tests/agent_eval 既有题集/分母完全独立。
"""

from __future__ import annotations

import json

from tests.experience_cases import runner


def test_dataset_schema_hash_and_check_registry() -> None:
    """题集治理：schema 版本、dataset_hash 与 cases 逐字节一致、E01-E14 连续、checks 已登记。"""
    dataset = runner.load_dataset()
    assert dataset["schema_version"] == 1
    ids = [case["id"] for case in dataset["cases"]]
    assert ids == [f"E{index:02d}" for index in range(1, 15)], f"题集必须是 E01-E14：{ids}"
    kinds = {case["kind"] for case in dataset["cases"]}
    assert kinds <= {"intake", "constraints", "editing", "streaming", "idempotency", "evidence"}
    for case in dataset["cases"]:
        assert case["title"], f"{case['id']} 缺输入/边界描述"
        assert case["checks"], f"{case['id']} 没有任何 check"
        for check in case["checks"]:
            assert check["name"] in runner.CHECKS, f"{case['id']} 引用了未登记的检查：{check['name']}"
    # hash 漂移检测必须真的能抓到改动：篡改一个 case 再算，应与登记 hash 不同
    mutated = json.loads(json.dumps(dataset["cases"], ensure_ascii=False))
    mutated[0]["title"] = mutated[0]["title"] + "（漂移探针）"
    assert runner.compute_dataset_hash(mutated) != dataset["dataset_hash"]


def test_all_experience_cases_pass_without_skips() -> None:
    """全量执行：本批 E01-E14 全部映射到已落地能力，不允许 fail，也不允许 skip。"""
    result = runner.run_all()
    failed = [
        (case["id"], check["name"], check["detail"])
        for case in result["cases"]
        for check in case["checks"]
        if check["status"] != runner.PASS
    ]
    assert not failed, f"体验题集存在未过检查：{json.dumps(failed, ensure_ascii=False, indent=1)[:2000]}"
    assert result["summary"]["checks"][runner.SKIP] == 0, "本批不允许显式 skip（能力未落地必须在报告列明）"
    runner.write_report(result)
