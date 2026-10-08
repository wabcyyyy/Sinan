"""band_samples.jsonl 采样器——深度下限重定的数据积累（AGENTS.md 2026-10-08 登记项）。

把一次真实 LLM 评测报告（llm_eval.py 的产物）的深度/转场带宽数字追加到
`report/nightly/band_samples.jsonl`。登记项的到期条款要求"攒够 ≥5 轮全量（13 例）
同口径样本后按带宽数值重定 coord/deeplink 下限"——样本带就是那 ≥5 行的载体。

口径（2026-10-08 拍板，当日夜审待人工区 1/2 号项，落死在这里避免每夜重问）：
- **只收全量样本**：case_count 必须等于标准数据集（eval_agent 的 cases.json）的
  用例数——报告刷新/冒烟类小样本 run 混进带里会让"≥5 行"算错带宽，一律拒绝；
- `terminal_reset_day_rate` / `terminal_reset_reasons` 取 run1 的用例均值/计数和
  （与 `depth_metrics` 同一 run1 口径）；报告没有这两项的顶层聚合，夜审首行样本
  即按此法复算，现固化为唯一口径；
- `generated_at` 沿抄被采样报告自带的生成时刻——样本记录的是"这次 run 长什么样"，
  不是采样动作发生的时刻；
- `generation_path` 必带：stream 与 graph 两条路径的深度口径不可混用（审查 P2-6），
  混行会让"≥5 行"算错带宽；同 (generated_at, generation_path) 重复采样拒绝追加。

用法：
    uv run python tests/agent_eval/sample_band.py --report report/nightly/llm_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SAMPLES_PATH = Path(__file__).resolve().parent / "report" / "nightly" / "band_samples.jsonl"


def build_row(report: dict, expected_cases: int | None = None) -> dict:
    """从 llm_eval 报告提取一行带宽样本（口径见模块 docstring）。

    expected_cases 缺省时按标准数据集（eval_agent.CASES_PATH）的用例数校验全量。
    """
    details = report.get("details")
    if not details:
        raise ValueError("报告缺 details：不是 llm_eval 的产物")
    if not report.get("generation_path"):
        raise ValueError("报告缺 generation_path：stream/graph 深度口径不可混用，拒绝采样")
    if expected_cases is None:
        from tests.agent_eval.eval_agent import CASES_PATH

        expected_cases = len(json.loads(CASES_PATH.read_text(encoding="utf-8")))
    if report.get("case_count") != expected_cases:
        raise ValueError(
            f"非全量样本：case_count={report.get('case_count')} ≠ 标准数据集 {expected_cases}，"
            "登记条款只收全量行，小样本 run 不得入带"
        )
    quality = [((detail.get("run1") or {}).get("quality") or {}) for detail in details]
    reasons: Counter[str] = Counter()
    for q in quality:
        reasons.update(q.get("terminal_reset_reasons") or {})
    rates = [float(q.get("terminal_reset_day_rate") or 0) for q in quality]
    depth = report.get("depth_metrics") or {}
    return {
        "generated_at": report["generated_at"],
        "prompt_version": report["prompt_version"],
        "generation_path": report["generation_path"],
        "case_count": report["case_count"],
        "coord_valid_rate": depth.get("coord_valid_rate"),
        "deeplink_resolvable_rate": depth.get("deeplink_resolvable_rate"),
        "category_reasonable_rate": depth.get("category_reasonable_rate"),
        "terminal_reset_day_rate": round(sum(rates) / max(len(rates), 1), 4),
        "terminal_reset_reasons": dict(sorted(reasons.items())),
    }


def append_sample(samples_path: Path, row: dict) -> bool:
    """追加一行样本；同 (generated_at, generation_path) 已存在时拒绝（防重复行污染带宽）。"""
    if samples_path.exists():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            old = json.loads(line)
            if old.get("generated_at") == row["generated_at"] and old.get("generation_path") == row["generation_path"]:
                return False
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：样本带入仓做证据，字节形态必须与平台无关（同 export_contracts 教训）
    with samples_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="把 llm_eval 报告的带宽样本追加进 band_samples.jsonl")
    parser.add_argument("--report", required=True, help="llm_eval 报告路径（如 report/nightly/llm_report.json）")
    parser.add_argument("--samples", default=str(SAMPLES_PATH), help="样本带路径（默认仓内 nightly 位置）")
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    row = build_row(report)
    if append_sample(Path(args.samples), row):
        print(f"已追加样本：{args.samples}（{row['generated_at']} / {row['generation_path']}）")
    else:
        print("该报告已采样过，跳过（同 generated_at + generation_path）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
