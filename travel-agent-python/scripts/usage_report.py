"""L5a 用量报告：按 run_id 聚合 llm_usage，回答「一次 N 天生成花多少调用/token/秒」。

口径（P1-7 之后）：
- llm_client 每次调用都落 usage_store（scene/model/tokens/duration/run_id）；
  生成主链路的 run_id 即行程 action_id，按 run 聚合即可对账到单趟行程。
- 离线评测（mock 装置）在 llm_client 之上的接缝就把 LLM 换掉了——探针实证
  calls=0：离线世界结构性没有 LLM 成本，因此成本棘轮只能挂在真实链路上。
- run_id 为 NULL 的记录（clarify / chat 等无 run 作用域入口）单独聚合成一行，
  不与生成 run 混算。

用法（travel-agent-python 目录下）：
    uv run python scripts/usage_report.py                  # 最近 10 个 run 的表
    uv run python scripts/usage_report.py --limit 3 --markdown
    uv run python scripts/usage_report.py --check --latest 3 --max-calls 30 --max-total-tokens 120000
      # nightly llm_eval 之后挂软棘轮：最近 N 个 run 的合计超限即退出 1
      #（LLM 有方差，超限按新基线改 yml 数字认账，不做硬精度门）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.common.config import settings  # noqa: E402

_NO_RUN = "（无 run 上下文）"


def _emit(line: str) -> None:
    """按控制台编码安全输出（同 eval_ratchet.py：GBK 码页下中文不能把门禁炸掉）。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(line.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def fetch_runs(db_path: str | Path, limit: int) -> dict[str, dict]:
    """按 run_id 聚合最近 limit 个 run（NULL run_id 归入哨兵键），新→旧。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(run_id, ?) AS run, COUNT(*) AS calls,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(duration_ms) AS duration_ms,
                   MIN(ts) AS first_ts,
                   GROUP_CONCAT(DISTINCT scene) AS scenes
            FROM llm_calls
            GROUP BY run
            ORDER BY first_ts DESC
            LIMIT ?
            """,
            (_NO_RUN, limit),
        ).fetchall()
    finally:
        conn.close()
    return {
        str(row["run"]): {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
            "total_tokens": int(row["prompt_tokens"] or 0) + int(row["completion_tokens"] or 0),
            "duration_ms": int(row["duration_ms"] or 0),
            "first_ts": int(row["first_ts"] or 0),
            "scenes": row["scenes"] or "other",
        }
        for row in rows
    }


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def render_table(runs: dict[str, dict]) -> list[str]:
    lines = [
        f"{'run':<18} {'调':>3} {'prompt':>8} {'compl':>7} {'合计':>8} {'秒':>7}  {'scenes':<24} {'首调':>11}",
    ]
    for run, row in runs.items():
        lines.append(
            f"{run[:16]:<18} {row['calls']:>3} {row['prompt_tokens']:>8} {row['completion_tokens']:>7}"
            f" {row['total_tokens']:>8} {row['duration_ms'] / 1000:>7.1f}  {row['scenes'][:24]:<24}"
            f" {_iso(row['first_ts']):>11}"
        )
    return lines


def render_markdown(runs: dict[str, dict]) -> list[str]:
    lines = [
        "| run | 调用 | prompt | completion | total | 秒 | 首调 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run, row in runs.items():
        lines.append(
            f"| `{run[:16]}` | {row['calls']} | {row['prompt_tokens']} | {row['completion_tokens']}"
            f" | {row['total_tokens']} | {row['duration_ms'] / 1000:.1f} | {_iso(row['first_ts'])} |"
        )
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="L5a 用量报告（按 run 聚合 llm_usage）")
    parser.add_argument("--limit", type=int, default=10, help="最近 N 个 run（默认 10）")
    parser.add_argument("--markdown", action="store_true", help="输出 Markdown 表（贴 README 用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="软棘轮：最近 --latest 个 run 的合计超过 --max-calls/--max-total-tokens 即退出 1",
    )
    parser.add_argument("--latest", type=int, default=1, help="--check 参与合计的最近 run 数")
    parser.add_argument("--max-calls", type=int, help="合计调用上限")
    parser.add_argument("--max-total-tokens", type=int, help="合计 token 上限")
    args = parser.parse_args(argv[1:])

    runs = fetch_runs(settings.usage_db_path, args.limit)

    if args.check:
        if not args.max_calls and not args.max_total_tokens:
            _emit("usage --check 需要 --max-calls 与/或 --max-total-tokens")
            return 1
        picked = dict(list(runs.items())[: max(args.latest, 1)])
        if len(picked) < args.latest:
            _emit(f"usage --check：库里只有 {len(picked)} 个 run，少于 --latest={args.latest}（样本不足，跳过）")
            return 0
        calls = sum(row["calls"] for row in picked.values())
        tokens = sum(row["total_tokens"] for row in picked.values())
        _emit(f"usage check：最近 {len(picked)} 个 run 合计 calls={calls} tokens={tokens}")
        problems = []
        if args.max_calls and calls > args.max_calls:
            problems.append(f"calls {calls} > 上限 {args.max_calls}")
        if args.max_total_tokens and tokens > args.max_total_tokens:
            problems.append(f"total_tokens {tokens} > 上限 {args.max_total_tokens}")
        if problems:
            _emit("usage 超限（成本棘轮）：查明原因后改 nightly yml 里的上限数字认账")
            for problem in problems:
                _emit(f"  {problem}")
            return 1
        _emit("usage check OK：在基线内")
        return 0

    if not runs:
        _emit("usage 库为空（还没有任何 LLM 调用记录）")
        return 0
    if args.json:
        _emit(json.dumps(runs, ensure_ascii=False, indent=2))
        return 0
    for line in render_table(runs):
        _emit(line)
    if args.markdown:
        _emit("")
        for line in render_markdown(runs):
            _emit(line)
    if _NO_RUN in runs:
        _emit(f"提示：{_NO_RUN} 的记录（clarify/chat 等入口）不与生成 run 混算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
