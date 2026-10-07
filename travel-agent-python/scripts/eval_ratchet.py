"""评测指标棘轮（PR-0）：逐指标双向棘轮，取代报告字节比对作行为防倒退门禁。

用法（travel-agent-python 目录下）：
    uv run python tests/agent_eval/eval_agent.py && uv run python tests/agent_eval/eval_research.py
    uv run python scripts/eval_ratchet.py             # 对照基线检查
    uv run python scripts/eval_ratchet.py --update    # 指标有意变化后重落基线（人工复核 diff！）

口径升级说明（取代 ci.yml 旧的 `git diff --exit-code tests/agent_eval/report/`）：
字节比对区分不了变好/变坏，任何 diff 都只能"无脑 re-baseline"；现在只对**指标**看涨跌，
非指标的报告噪声（耗时、摘要形态）不再逼人认账。可复现性由 CI `eval-determinism` job
（同 fixture 两遍 SHA256）单独把关——两件事不再混在一条 diff 里。

基线（`tests/agent_eval/metrics_baseline.json`）每个指标冻结一个界：
- 只带 `min` = 越高越好（下限）；只带 `max` = 越低越好（上限）；
- `min` + `max` = 钉在区间内（口径/结构计数类：状态占比、轮次、证据量，动了就该认账）；
- 可选 `tolerance`（默认 1e-9）：离线报告确定可复现，容差只吸收浮点噪声。

双向棘轮（与 scripts/typecheck.py 同语义：基线只跟不松）：
- 回归（低于下限 / 越过上限）→ 红；
- 显著变好（越高越好越过上限线 / 越低越好跌破下限线）→ 也红，逼 `--update` 认账
  把基线收紧——否则"改进"被遗忘后，倒退能藏在旧基线后面；
- 报告里出现未冻结的新指标 / 基线里有报告没有的指标 → 红（改指标口径必须认账）。

`--update` 按当前报告重写界值（保留方向键与 note）。**放宽界值属 INV-1 豁免**：
diff 里 ↑/↓ 已标出方向，放宽必须在 PR/AGENTS.md 留注释与期限。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "agent_eval" / "metrics_baseline.json"

DEFAULT_TOLERANCE = 1e-9


def _emit(line: str) -> None:
    """按控制台编码安全输出（同 typecheck.py：GBK 码页下中文键不能把门禁炸掉）。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(line.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def load_reports(baseline: dict, base_dir: Path) -> dict[str, dict]:
    """按基线登记的报告路径读当前指标；缺失/形状不对记为 None（由 check 判红）。"""
    current: dict[str, dict] = {}
    for report_path in baseline.get("reports", {}):
        full = base_dir / report_path
        if not full.is_file():
            current[report_path] = {"metrics": None}
            continue
        document = json.loads(full.read_text(encoding="utf-8"))
        metrics = document.get("metrics")
        current[report_path] = {"metrics": metrics if isinstance(metrics, dict) else None}
    return current


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def check(current: dict[str, dict], baseline: dict) -> list[str]:
    """返回问题清单（空列表 = 通过）。纯函数，便于离线单测与变异验证。"""
    problems: list[str] = []
    reports = baseline.get("reports", {})
    for report_path, bounds_by_metric in reports.items():
        metrics = (current.get(report_path) or {}).get("metrics")
        if metrics is None:
            problems.append(f"{report_path}: 报告缺失或没有 metrics（先跑 eval_agent.py / eval_research.py）")
            continue
        frozen = set(bounds_by_metric)
        present = set(metrics)
        for key in sorted(frozen - present):
            problems.append(f"{report_path}: 基线指标 {key} 在报告里消失（改指标口径请 --update 认账）")
        for key in sorted(present - frozen):
            problems.append(f"{report_path}: 未冻结的新指标 {key}（跑 --update 认账后再入库）")
        for key in sorted(frozen & present):
            bounds = bounds_by_metric[key] or {}
            min_bound = _numeric(bounds.get("min"))
            max_bound = _numeric(bounds.get("max"))
            tolerance = _numeric(bounds.get("tolerance"))
            if tolerance is None:
                tolerance = DEFAULT_TOLERANCE
            value = _numeric(metrics[key])
            if min_bound is None and max_bound is None:
                problems.append(f"{report_path}: 基线 {key} 既无 min 也无 max（基线文件写坏了）")
                continue
            if value is None:
                problems.append(f"{report_path}: 指标 {key} 缺失或非数值：{metrics[key]!r}")
                continue
            if min_bound is not None and value < min_bound - tolerance:
                problems.append(f"{report_path}: {key} {value:g} < 下限 {min_bound:g}（回归）")
            if max_bound is not None and value > max_bound + tolerance:
                problems.append(f"{report_path}: {key} {value:g} > 上限 {max_bound:g}（回归/口径漂移）")
            if min_bound is not None and max_bound is None and value > min_bound + tolerance:
                problems.append(
                    f"{report_path}: {key} {value:g} 显著高于下限 {min_bound:g}（变好）——"
                    "跑 --update 认账收紧基线，别让改进被遗忘"
                )
            if max_bound is not None and min_bound is None and value < max_bound - tolerance:
                problems.append(
                    f"{report_path}: {key} {value:g} 显著低于上限 {max_bound:g}（变好）——"
                    "跑 --update 认账收紧基线，别让改进被遗忘"
                )
    return problems


def update(current: dict[str, dict], baseline: dict) -> dict:
    """按当前指标重写界值（保留方向键与 note）；返回新基线文档。

    只改数值界：min/max 各自冻结为当前值。区间带宽如需保留请手改并在 PR 说明。
    """
    reports: dict[str, dict] = {}
    for report_path, bounds_by_metric in baseline.get("reports", {}).items():
        metrics = (current.get(report_path) or {}).get("metrics") or {}
        rewritten: dict[str, dict] = {}
        for key, bounds in bounds_by_metric.items():
            entry = dict(bounds or {})
            value = _numeric(metrics.get(key))
            if value is not None:
                if "min" in entry:
                    entry["min"] = value
                if "max" in entry:
                    entry["max"] = value
            rewritten[key] = entry
        # --update 后新指标也要冻结：min-only 起步（变好会再逼一次认账，语义安全）
        for key in sorted(set(metrics) - set(bounds_by_metric)):
            rewritten[key] = {"min": metrics[key], "note": "自动冻结（--update 新增）；请人工补方向与 note"}
        reports[report_path] = rewritten
    return {**{k: v for k, v in baseline.items() if k != "reports"}, "reports": reports}


def _describe_movement(old: Any, new: Any, bound: str) -> str:
    """界值移动方向的口径提示：min 上移/下移 = 收紧/放宽；max 相反。"""
    old_value, new_value = _numeric(old), _numeric(new)
    if old_value is None or new_value is None:
        return "（新冻结）"
    tightened = new_value > old_value if bound == "min" else new_value < old_value
    if new_value == old_value:
        return "（不变）"
    return "（收紧）" if tightened else "（↓ 放宽属 INV-1 豁免，PR 须留注释与期限）"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="eval 指标棘轮（行为防倒退）")
    parser.add_argument("--update", action="store_true", help="按当前报告重写基线（人工复核 diff 后入库）")
    parser.add_argument("--baseline", default=str(BASELINE), help="基线文件（报告路径相对其所在目录解析）")
    args = parser.parse_args(argv[1:])
    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        _emit(f"未找到指标基线：{baseline_path}（先跑 --update 冻结首版）")
        return 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    current = load_reports(baseline, baseline_path.parent)

    if args.update:
        rewritten = update(current, baseline)
        baseline_path.write_text(
            json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        for report_path, bounds_by_metric in rewritten["reports"].items():
            old_bounds = (baseline.get("reports") or {}).get(report_path) or {}
            for key, entry in bounds_by_metric.items():
                for bound in ("min", "max"):
                    if bound not in entry:
                        continue
                    old = (old_bounds.get(key) or {}).get(bound)
                    note = _describe_movement(old, entry[bound], bound)
                    _emit(f"  {report_path} {key}.{bound}: {old!r} -> {entry[bound]!r} {note}")
        _emit(f"baseline updated: {baseline_path}（人工复核上面的 diff 再提交；放宽属 INV-1 豁免）")
        return 0

    problems = check(current, baseline)
    if problems:
        _emit(f"\nNEW eval regressions / unfrozen metrics ({len(problems)}):")
        for problem in problems:
            _emit(f"  {problem}")
        _emit("\n回归先修；有意改口径跑 `scripts/eval_ratchet.py --update` 认账（INV-1：放宽须留注释与期限）")
        return 1
    total = sum(len(bounds) for bounds in (baseline.get("reports") or {}).values())
    _emit(f"eval ratchet OK: {total} 个指标在基线内（基线只跟不松）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
