"""LLM-as-judge + 人工校准集（PR-7 / D7）：四维评分与 judge-human 一致性。

纪律（D7，机检在 tests/test_llm_judge.py）：
- judge **仅 nightly**，不进 PR 硬门禁——judge 有方差，硬门禁只会造 flaky 与
  「对 judge 调参」的内卷；PR 门禁的结构指标（时间冲突/接地率等）仍由确定性代码负责，
  judge 只评语言与标注质量；
- judge 与被评模型**不同家族**（`assert_judge_family_distinct` 配置校验），
  防 self-preference；
- 一致性证据：30 条人工校准集（calibration/human_scores.jsonl）报 judge-human
  一致性——三个序数维（忠实度/相关性/连贯性）用 Spearman ρ，estimated 标注
  正确性（三分类）用 Cohen's κ。

用法（nightly）：`uv run python tests/agent_eval/judge.py`
产出 `report/nightly/judge_report.json|md`：一致性指标 + 每样本对照，元数据带
judge prompt 版本与哈希、题集哈希、judge/被评模型名。无凭据时如实跳过（不冒充）。

judge 评分用结构化输出档（守约网关 json_schema 强约束，PR-5 档；DeepSeek 官方等拒
json_schema 的网关降级 json_object，与生成出口同一能力位）+ 单次修复重试。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.agent.core.json_utils import LlmJsonError, parse_llm_json
from app.common import model_registry
from app.common.llm_client import get_judge_client

CALIBRATION_PATH = Path(__file__).parent / "calibration" / "human_scores.jsonl"
REPORT_DIR = Path(__file__).parent / "report" / "nightly"

#: judge prompt 版本（改 rubric 必须升版本，报告元数据记录版本与哈希）
JUDGE_PROMPT_VERSION = "judge-v1.0-20260924"

_JUDGE_RUBRIC = (
    "你是一名行程规划质量评审员。给定用户旅行意图与一条候选行程片段，按四个维度评分。"
    "你只评语言与标注质量：行程的结构性问题（时间冲突、预算、深链等）由确定性校验器负责。\n"
    "1. faithfulness（忠实度，1-5 整数）：内容是否可信——点位是否像真实存在的正式名称、"
    "叙述是否编造具体事实（虚构店名/虚构景点/编造历史细节即低分）。\n"
    "2. relevance（相关性，1-5 整数）：安排是否贴合用户旅行意图（主题、节奏、人群）。\n"
    "3. coherence（连贯性，1-5 整数）：主题句、时间安排与叙述是否通顺自洽"
    "（前后矛盾或主题与内容脱节即低分）。\n"
    "4. estimated_labels（estimated 标注正确性，三分类）：条目的 value_kind / "
    "verification_status 标注是否与内容的确定性匹配——自称 verified 却给出无从核实的"
    "精确数值 = incorrect；该标 estimated 却标 verified 同理；介于两者之间 = partial；"
    "标注与内容确定性匹配 = correct。\n"
    '只输出一个 JSON 对象：{"faithfulness": 1到5的整数, "relevance": 1到5的整数, '
    '"coherence": 1到5的整数, "estimated_labels": "correct|partial|incorrect", '
    '"rationale": "不超过80字的理由"}。'
)

_JUDGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "faithfulness": {"type": "integer", "minimum": 1, "maximum": 5},
        "relevance": {"type": "integer", "minimum": 1, "maximum": 5},
        "coherence": {"type": "integer", "minimum": 1, "maximum": 5},
        "estimated_labels": {"type": "string", "enum": ["correct", "partial", "incorrect"]},
        "rationale": {"type": "string"},
    },
    "required": ["faithfulness", "relevance", "coherence", "estimated_labels", "rationale"],
    "additionalProperties": False,
}

_ORDINAL_DIMS = ("faithfulness", "relevance", "coherence")
_LABELS_FIELD = "estimated_labels"
_LABEL_CATEGORIES = ("correct", "partial", "incorrect")


class JudgeScores(NamedTuple):
    faithfulness: int
    relevance: int
    coherence: int
    estimated_labels: str
    rationale: str


def judge_prompt_sha256() -> str:
    """judge prompt 的内容哈希（报告元数据用：口径变更可归因）。"""
    return hashlib.sha256(_JUDGE_RUBRIC.encode("utf-8")).hexdigest()


def judge_family(model: str) -> str:
    """模型家族 = 供应商系名词干（qwen-plus / qwen3-max → qwen；deepseek-v3 → deepseek）。

    词干去尾部数字（qwen3 与 qwen 是同家族——版本后缀不换血统，D7 的防
    self-preference 底线）。
    """
    stem = str(model or "").strip().lower().split("-", 1)[0]
    return re.sub(r"\d+$", "", stem)


def assert_judge_family_distinct(judge_model: str, judged_model: str) -> None:
    """配置校验（D7）：judge 与被评模型必须异家族，防 self-preference。"""
    if not judge_model or not judged_model:
        raise ValueError("judge 模型与被评模型都必须显式配置")
    if judge_family(judge_model) == judge_family(judged_model):
        raise ValueError(f"judge 与被评模型同家族（{judge_family(judge_model)}）：异家族才防 self-preference（D7）")


def load_calibration(path: Path = CALIBRATION_PATH) -> list[dict]:
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return samples


def calibration_meta(path: Path = CALIBRATION_PATH) -> dict:
    return {
        "calibration_file": f"calibration/{path.name}",
        "calibration_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _ranks(values: list[float]) -> list[float]:
    """平均秩（并列取均值）：Spearman 的秩变换。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1  # 1-based 平均秩
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关 ρ；样本 <2 或任一列为常量（无秩方差）时无定义，返回 None。"""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx, ry = _ranks(list(xs)), _ranks(list(ys))
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return round(cov / (vx * vy) ** 0.5, 4)


def cohens_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's κ（两分类器一致度，扣除随机一致）；完全一致回 1.0，无样本/单方常量分歧无定义。"""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    if po >= 1.0:
        return 1.0
    categories = set(a) | set(b)
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in categories)
    if 1 - pe <= 0:
        return None
    return round((po - pe) / (1 - pe), 4)


def _decode_judge_json(raw: object) -> dict:
    """judge 边界解码（D3 兜底档）：严格解析优先；网关无视 response_format 时
    （deepseek 系实测：一律裹 ```json 围栏）剥**一层完整围栏**再严格解析。

    与 PR-5 的 parse_llm_json 不冲突：那条红线管的是生成主链（网关守约、纯 JSON），
    这里是异家族 judge 的边界适配——**不做首末大括号截取**（半截真相不救活），
    只接受完整围栏文档；解析结果随后过 _JUDGE_SCHEMA 客户端校验
    （schema-in-prompt + 校验 + 修复重试 = decide.py 已验证模式的组合）。
    """
    try:
        return parse_llm_json(raw)
    except LlmJsonError:
        text = str(raw or "").strip()
        match = re.fullmatch(r"```[a-zA-Z]*\s*(.*?)\s*```", text, re.S)
        if match is None:
            raise
        return parse_llm_json(match.group(1))


def _scores_from(raw: object) -> JudgeScores:
    """解码 + schema 客户端校验 + 字段收拢；任一步失败抛 LlmJsonError（喂修复层）。"""
    data = _decode_judge_json(raw)
    error = Draft202012Validator(_JUDGE_SCHEMA).iter_errors(data)
    first = next(iter(error), None)
    if first is not None:
        raise LlmJsonError(f"judge 输出不符合评分 schema：{first.message}")
    return JudgeScores(
        faithfulness=int(data["faithfulness"]),
        relevance=int(data["relevance"]),
        coherence=int(data["coherence"]),
        estimated_labels=str(data[_LABELS_FIELD]),
        rationale=str(data.get("rationale") or ""),
    )


def _judge_response_format() -> dict:
    """judge 结构化输出档按网关能力位分支（与生成出口同一判据，夜审 R2-F1）。

    DeepSeek 官方 2026-10-05 实测对 json_schema 400（同 key json_object 200）——
    judge 角色默认落 main（本部署即 DeepSeek），不分支则 nightly 评分整轮失败。
    降级 json_object 后结构约束靠 _JUDGE_RUBRIC 内嵌的输出形状 + 修复重试 +
    本地 _scores_from 的 schema 校验兜住。
    """
    if "json_schema" in model_registry.binding("judge").capabilities():
        return {"type": "json_schema", "json_schema": {"name": "judge_scores", "strict": True, "schema": _JUDGE_SCHEMA}}
    return {"type": "json_object"}


def judge_sample(sample: dict, client) -> JudgeScores:
    """judge 一次评分：按网关能力选结构化输出档（守约网关即强约束）+ 修复重试 + 客户端校验。"""
    payload = {"intent": sample.get("intent"), "plan": sample.get("plan")}
    response_format = _judge_response_format()
    messages = [
        {"role": "system", "content": _JUDGE_RUBRIC},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    raw = client.chat(messages, temperature=0, max_tokens=400, response_format=response_format)
    try:
        return _scores_from(raw)
    except LlmJsonError:
        repaired = client.chat(
            [
                {"role": "system", "content": "修复下面的 JSON：保持原意，只输出一个语法正确的 JSON 对象，不要解释。"},
                {"role": "user", "content": str(raw or "")[:12000]},
            ],
            temperature=0,
            max_tokens=400,
            response_format=response_format,
        )
        return _scores_from(repaired)


def _agreement(judged: list[dict], human: list[dict]) -> dict:
    agreement: dict = {"spearman": {}, "cohens_kappa": {}, "exact_match_rate": {}}
    for dim in _ORDINAL_DIMS:
        xs = [float(h[dim]) for h in human]
        ys = [float(j[dim]) for j in judged]
        agreement["spearman"][dim] = spearman(xs, ys)
        agreement["exact_match_rate"][dim] = round(
            sum(1 for x, y in zip(xs, ys, strict=True) if x == y) / max(len(xs), 1), 4
        )
    human_labels = [str(h[_LABELS_FIELD]) for h in human]
    judged_labels = [str(j[_LABELS_FIELD]) for j in judged]
    agreement["cohens_kappa"][_LABELS_FIELD] = cohens_kappa(human_labels, judged_labels)
    agreement["exact_match_rate"][_LABELS_FIELD] = round(
        sum(1 for x, y in zip(human_labels, judged_labels, strict=True) if x == y) / max(len(human_labels), 1), 4
    )
    return agreement


def judge_dataset(samples: list[dict], *, client, judge_model: str, judged_model: str) -> dict:
    """judge 全部样本并对照人工分：一致性指标 + 逐样本明细 + 口径元数据。"""
    judged: list[dict] = []
    human: list[dict] = []
    details: list[dict] = []
    errors = 0
    for sample in samples:
        try:
            scores = judge_sample(sample, client)
        except LlmJsonError as exc:
            # 单样本 judge 失败不毁掉整份报告：记录并剔出一致性分母（judged_count 入报告）
            errors += 1
            details.append(
                {"id": sample.get("id"), "human": dict(sample["human"]), "judge": None, "error": str(exc)[:160]}
            )
            continue
        row = scores._asdict()
        judged.append(row)
        human.append(dict(sample["human"]))
        details.append(
            {"id": sample.get("id"), "human": dict(sample["human"]), "judge": row, "note": sample.get("note")}
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "judge_model": judge_model,
        "judged_model": judged_model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "judge_prompt_sha256": judge_prompt_sha256(),
        "calibration_provenance": (
            "校准样本与人工评分由维护者代拟（2026-09-24）；真实人工复评可覆盖同批样本后重算一致性"
        ),
        **calibration_meta(),
        "sample_count": len(samples),
        "judged_count": len(judged),
        "judge_error_count": errors,
        "agreement": _agreement(judged, human),
        "details": details,
    }


def write_report(report: dict, report_dir: Path = REPORT_DIR) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "judge_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agreement = report.get("agreement") or {}
    lines = [
        "# LLM-as-judge 校准报告",
        "",
        f"- judge：`{report['judge_model']}` ｜ 被评：`{report['judged_model']}`"
        f"（异家族机检见 tests/test_llm_judge.py）",
        f"- judge prompt：`{report['judge_prompt_version']}`（sha256 {report['judge_prompt_sha256'][:12]}…）",
        f"- 校准集：`{report['calibration_file']}`（sha256 {report['calibration_sha256'][:12]}…，"
        f"{report['sample_count']} 条）",
        f"- 生成时间：{report['generated_at']} ｜ 仅 nightly（D7：不进 PR 门禁）",
        "",
        "| 维度 | Spearman ρ | Cohen κ | 逐值一致率 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for dim in (*_ORDINAL_DIMS, _LABELS_FIELD):
        rho = (agreement.get("spearman") or {}).get(dim)
        kappa = (agreement.get("cohens_kappa") or {}).get(dim)
        exact = (agreement.get("exact_match_rate") or {}).get(dim)
        shown_rho = "-" if rho is None else f"{rho:.3f}"
        shown_kappa = "-" if kappa is None else f"{kappa:.3f}"
        shown_exact = "-" if exact is None else f"{exact:.2%}"
        lines.append(f"| {dim} | {shown_rho} | {shown_kappa} | {shown_exact} |")
    (report_dir / "judge_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path


def main() -> int:
    judge_model = model_registry.model_for("judge")
    judged_model = model_registry.model_for("main")
    if not (model_registry.configured("judge") or model_registry.configured("main")):
        print("未配置 judge 凭据：跳过（不冒充结果）")
        return 2
    assert_judge_family_distinct(judge_model, judged_model)
    samples = load_calibration()
    report = judge_dataset(
        samples,
        client=get_judge_client(),
        judge_model=judge_model,
        judged_model=judged_model,
    )
    path = write_report(report)
    print(json.dumps(report["agreement"], ensure_ascii=False, indent=2))
    print(f"报告已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
