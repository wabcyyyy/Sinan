"""代码规模门禁：长函数与超长文件（G-2.6）。

用法（travel-agent-python 目录下）：
    uv run python scripts/code_metrics.py            # 报告 + 与上限比对
    uv run python scripts/code_metrics.py --update   # 用当前值收紧上限（只减不增）

判据（INV-1 门禁只升不降）：
- ≥70 行函数数量不得超过 `LIMITS["long_functions"]`；
- >400 行文件数量不得超过 `LIMITS["huge_files"]`（算法类文件另有豁免名额）。

为什么做成 ratchet 而不是一次性达标：存量长尾散布在 services/rag 等
P2 安全网**不覆盖**的目录（快照/eval 只覆盖生成链路），一次大扫除无法
用现有判据证明行为不变。冻结现值 + 只减不增，让每次顺带拆分都成为净收益，
而不是逼出一次无法验证的大重构。

豁免：纯算法/数据表文件（retriever 检索打分、schemas 线级契约）按 PLAN
约定占 2 个名额，不计入超长文件。
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

FUNCTION_LINES = 70
FILE_LINES = 400

# 算法/数据文件豁免名额（PLAN G-2.6：rag/retriever 等算法文件豁免 2 个）
EXEMPT_FILES = ("app/schemas/trip.py",)

LIMITS_PATH = pathlib.Path(__file__).with_name("code_metrics_limits.json")


def measure() -> dict:
    long_functions: list[dict] = []
    huge_files: list[dict] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(APP_DIR.parent).as_posix()
        lines = len(source.splitlines())
        if lines > FILE_LINES and rel not in EXEMPT_FILES:
            huge_files.append({"file": rel, "lines": lines})
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length >= FUNCTION_LINES:
                    long_functions.append({"file": rel, "name": node.name, "lines": length})
    long_functions.sort(key=lambda row: (-row["lines"], row["file"], row["name"]))
    huge_files.sort(key=lambda row: (-row["lines"], row["file"]))
    return {"long_functions": long_functions, "huge_files": huge_files}


def load_limits() -> dict:
    return json.loads(LIMITS_PATH.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="用当前实测值收紧上限")
    args = parser.parse_args()

    measured = measure()
    current = {"long_functions": len(measured["long_functions"]), "huge_files": len(measured["huge_files"])}

    if args.update:
        previous = load_limits() if LIMITS_PATH.exists() else {}
        shrunk = {key: min(current[key], previous.get(key, current[key])) for key in current}
        LIMITS_PATH.write_text(json.dumps(shrunk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"limits updated: {shrunk}")
        return 0

    limits = load_limits()
    print(f"== long functions (>={FUNCTION_LINES} lines) ==")
    for row in measured["long_functions"]:
        print(f"  {row['lines']:4d}  {row['file']}::{row['name']}")
    print(f"count: {current['long_functions']} (limit {limits['long_functions']})")

    exempts = ", ".join(EXEMPT_FILES)
    print(f"\n== huge files (>{FILE_LINES} lines, exempts: {exempts}) ==")
    for row in measured["huge_files"]:
        print(f"  {row['lines']:4d}  {row['file']}")
    print(f"count: {current['huge_files']} (limit {limits['huge_files']})")

    regressions = [key for key in current if current[key] > limits[key]]
    if regressions:
        print("\nFAIL: 规模门禁回退（INV-1 只减不增）：")
        for key in regressions:
            print(f"  {key}: {current[key]} > {limits[key]}")
        return 1
    print("\nOK: 规模门禁未回退")
    return 0


if __name__ == "__main__":
    sys.exit(main())
