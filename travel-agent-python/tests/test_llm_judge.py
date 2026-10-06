"""LLM-as-judge 单测（PR-7 / D7）：一致性口径、异家族机检、D7 边界。

judge 只评语言/标注质量且仅 nightly（D7）。本文件是 D7 的机检落点：
ci.yml（PR 硬门禁）不得出现 judge；nightly.yml 必须有 judge job；judge 与被评
模型必须异家族（防 self-preference），默认配置也受此约束。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.core.json_utils import LlmJsonError
from app.common import model_registry
from app.common.config import settings
from tests.agent_eval.judge import (
    assert_judge_family_distinct,
    calibration_meta,
    cohens_kappa,
    judge_dataset,
    judge_family,
    judge_prompt_sha256,
    judge_sample,
    load_calibration,
    spearman,
    write_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
NIGHTLY_YML = REPO_ROOT / ".github" / "workflows" / "nightly.yml"


class _FakeJudgeClient:
    """按脚本逐次给分的 judge 替身；记录 response_format 供契约钉。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def _reply(faithfulness: int, relevance: int, coherence: int, labels: str) -> str:
    return json.dumps(
        {
            "faithfulness": faithfulness,
            "relevance": relevance,
            "coherence": coherence,
            "estimated_labels": labels,
            "rationale": "测试脚本产出",
        },
        ensure_ascii=False,
    )


def _human_reply(human: dict) -> str:
    return _reply(human["faithfulness"], human["relevance"], human["coherence"], human["estimated_labels"])


class TestAgreementMath:
    def test_spearman_perfect_inverse_and_ties(self):
        assert spearman([1, 2, 3], [10, 20, 30]) == 1.0
        assert spearman([1, 2, 3], [30, 20, 10]) == -1.0
        # 并列取平均秩：单调一致仍为 1
        assert spearman([1, 1, 2], [5, 5, 9]) == 1.0
        assert spearman([1, 1], [1, 2]) is None  # 无秩方差，无定义
        assert spearman([1], [1]) is None

    def test_kappa_perfect_chance_and_single_category(self):
        assert cohens_kappa(["correct"] * 5, ["correct"] * 5) == 1.0
        # 完全随机一致（双方各半对开且互相错开）：κ ≤ 0
        kappa = cohens_kappa(["correct", "incorrect"], ["incorrect", "correct"])
        assert kappa is not None and kappa <= 0
        # 部分一致（一致率 0.75 > 随机期望 0.5）落在 (0, 1)：κ = 0.5
        mid = cohens_kappa(
            ["correct", "correct", "incorrect", "incorrect"],
            ["correct", "correct", "correct", "incorrect"],
        )
        assert mid is not None and 0 < mid < 1
        assert cohens_kappa([], []) is None


class TestCalibrationSet:
    def test_calibration_has_30_valid_entries(self):
        samples = load_calibration()
        assert len(samples) == 30
        ids = [row["id"] for row in samples]
        assert len(set(ids)) == 30
        for row in samples:
            human = row["human"]
            assert set(human) == {"faithfulness", "relevance", "coherence", "estimated_labels"}
            assert all(1 <= int(human[dim]) <= 5 for dim in ("faithfulness", "relevance", "coherence"))
            assert human["estimated_labels"] in ("correct", "partial", "incorrect")
            assert row.get("intent") and row.get("plan") and row.get("note")

    def test_calibration_hash_is_recordable(self):
        meta = calibration_meta()
        assert meta["calibration_file"] == "calibration/human_scores.jsonl"
        assert len(meta["calibration_sha256"]) == 64


class TestJudgeCalls:
    def test_judge_sample_pins_json_schema_strict(self, monkeypatch):
        """守约网关（百炼兼容层）下 judge 仍钉 json_schema strict。

        旧口径"无条件钉 json_schema"在本机唯一 LLM 通道 DeepSeek 官方必然整轮
        失败（2026-10-05 实测对该档 400、同 key json_object 200，夜审 R2-F1）——
        钉子改为钉能力位分支的守约方向，降级方向见下一条。
        """
        monkeypatch.setattr(
            settings, "llm_provider_dashscope_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "dash-key")
        monkeypatch.setattr(settings, "llm_role_judge", "dashscope:qwen-max")
        client = _FakeJudgeClient([_reply(5, 4, 3, "partial")])
        scores = judge_sample({"intent": "i", "plan": []}, client)
        assert scores == (5, 4, 3, "partial", "测试脚本产出")
        fmt = client.calls[0]["response_format"]
        assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True

    def test_judge_sample_degrades_to_json_object_on_deepseek(self, monkeypatch):
        """DeepSeek 官方（judge 角色默认落 main）无 json_schema 能力位：降级 json_object，
        不得再发必 400 的强约束档（R2-F1）。"""
        monkeypatch.setattr(settings, "llm_base_url", "https://api.deepseek.com")
        monkeypatch.setattr(settings, "llm_role_judge", "")
        client = _FakeJudgeClient([_reply(5, 4, 3, "partial")])
        judge_sample({"intent": "i", "plan": []}, client)
        assert client.calls[0]["response_format"] == {"type": "json_object"}

    def test_bad_judge_json_goes_through_repair_retry(self):
        client = _FakeJudgeClient(['{"faithfulness": 5, ', _reply(5, 5, 5, "correct")])
        scores = judge_sample({"intent": "i", "plan": []}, client)
        assert scores.coherence == 5
        assert len(client.calls) == 2, "坏 JSON 应触发一次修复重试（PR-5 三层的中层）"

    def test_fenced_output_unwraps_without_repair_but_truncated_does_not(self):
        """D3 兜底档：deepseek 系无视 response_format、只裹完整围栏——整层剥离；
        截断/半截文档不打捞（不做首末大括号截取），走修复重试。
        """
        fenced = "```json\n" + _reply(4, 3, 2, "partial") + "\n```"
        client = _FakeJudgeClient([fenced])
        scores = judge_sample({"intent": "i", "plan": []}, client)
        assert (scores.faithfulness, scores.estimated_labels) == (4, "partial")
        assert len(client.calls) == 1, "完整围栏直接解码，无需修复"

        half = '{"faithfulness": 4, "relevance"'
        client = _FakeJudgeClient([half, _reply(1, 1, 1, "incorrect")])
        judge_sample({"intent": "i", "plan": []}, client)
        assert len(client.calls) == 2, "半截文档必须走修复，禁止截取救活"

    def test_schema_violating_scores_are_rejected_even_after_repair(self):
        violating = json.dumps(
            {"faithfulness": 9, "relevance": 5, "coherence": 5, "estimated_labels": "correct", "rationale": "x"}
        )
        with pytest.raises(LlmJsonError, match="schema"):
            judge_sample({"intent": "i", "plan": []}, _FakeJudgeClient([violating, violating]))

    def test_report_includes_agreement_prompt_hash_and_details(self, tmp_path: Path):
        samples = load_calibration()
        client = _FakeJudgeClient([_human_reply(row["human"]) for row in samples])
        report = judge_dataset(samples, client=client, judge_model="deepseek-v3", judged_model="qwen-plus")
        agreement = report["agreement"]
        assert agreement["spearman"] == {"faithfulness": 1.0, "relevance": 1.0, "coherence": 1.0}
        assert agreement["cohens_kappa"]["estimated_labels"] == 1.0
        assert report["judge_prompt_sha256"] == judge_prompt_sha256() and len(report["judge_prompt_sha256"]) == 64
        assert report["judge_prompt_version"]
        assert report["calibration_sha256"] == calibration_meta()["calibration_sha256"]
        assert report["sample_count"] == 30 and len(report["details"]) == 30

        path = write_report(report, report_dir=tmp_path)
        assert path.is_file() and (tmp_path / "judge_report.md").is_file()
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert reloaded["agreement"]["spearman"]["faithfulness"] == 1.0

    def test_mismatched_judge_lowers_kappa_not_spearman_shape(self):
        samples = load_calibration()
        flipped = [_reply(5, 5, 5, "incorrect")] * len(samples)
        report = judge_dataset(
            samples, client=_FakeJudgeClient(flipped), judge_model="deepseek-v3", judged_model="qwen-plus"
        )
        assert report["agreement"]["cohens_kappa"]["estimated_labels"] != 1.0


class TestD7Guards:
    def test_judge_family_distinct_constraint(self):
        assert judge_family("deepseek-v3") == "deepseek"
        assert judge_family("qwen-plus") == "qwen"
        assert_judge_family_distinct("deepseek-v3", "qwen-plus")  # 异家族通过
        with pytest.raises(ValueError, match="同家族"):
            assert_judge_family_distinct("qwen3-max", "qwen-plus")
        with pytest.raises(ValueError, match="同家族"):
            assert_judge_family_distinct("deepseek-v3", "deepseek-r1")
        with pytest.raises(ValueError):
            assert_judge_family_distinct("", "qwen-plus")

    def test_judge_role_defaults_to_main_and_is_guarded(self):
        """D7 的默认口径（2026-10-04 走注册表后）：judge 角色留空即复用 main。

        默认不再"自带一个异家族模型"（那要求默认就配一个能服务它的网关，做不到就
        必然 404），而是由 `assert_judge_family_distinct` 在真跑 judge 时把"没配
        异家族通道"这件事响亮拦住——judge 是 nightly 专项，缺配置就不该冒充跑过。
        """
        assert model_registry.model_for("judge") == model_registry.model_for("main")
        with pytest.raises(ValueError, match="同家族"):
            assert_judge_family_distinct(model_registry.model_for("judge"), model_registry.model_for("main"))

    def test_judge_role_can_be_pointed_at_another_family(self, monkeypatch):
        """配了 `LLM_ROLE_JUDGE` 就必须生效——D7 的正解是异家族网关。"""
        monkeypatch.setattr(settings, "llm_role_judge", "dashscope:qwen-max")
        assert model_registry.model_for("judge") == "qwen-max"
        assert_judge_family_distinct(model_registry.model_for("judge"), model_registry.model_for("main"))

    def test_ci_hard_gates_contain_no_judge(self):
        """显式断言：PR 硬门禁（ci.yml）不含 judge——防有人「顺手」加进 PR 门禁（D7）。"""
        assert "judge" not in CI_YML.read_text(encoding="utf-8").lower()
        assert "judge" in NIGHTLY_YML.read_text(encoding="utf-8").lower(), "nightly 必须有 judge job"
