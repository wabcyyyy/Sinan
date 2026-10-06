"""pyright 基线 ratchet（G-0.3）：存量报错冻结在 pyright-baseline.txt，只减不增（INV-1）。

用法：
    uv run python scripts/typecheck.py           # 对照基线检查；出现基线外的新报错 → exit 1
    uv run python scripts/typecheck.py --update  # 重新生成基线（修掉存量后用来记录进展）

基线键 = `文件路径:规则:消息`（不含行列号）：格式化/移动代码不会误报"新增"，
只有真正的类型错误（或措辞变化）才会被拦下。修好存量后请跑 --update 让基线只减不增。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "pyright-baseline.txt"


def current_errors() -> list[str]:
    proc = subprocess.run(
        ["uv", "run", "pyright", "--outputjson"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if not proc.stdout.strip():
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("pyright produced no JSON output")
    data = json.loads(proc.stdout)
    keys: list[str] = []
    for diag in data.get("generalDiagnostics", []):
        if diag.get("severity") != "error":
            continue
        file = str(diag.get("file", "")).replace("\\", "/")
        root = str(ROOT).replace("\\", "/")
        if file.lower().startswith(root.lower() + "/"):
            file = file[len(root) + 1 :]
        rule = diag.get("rule") or "no-rule"
        # 消息含多行子诊断：压平换行，保证一个报错 = 基线里一行
        message = " | ".join(str(diag.get("message", "")).splitlines())
        keys.append(f"{file}:{rule}:{message}")
    return sorted(set(keys))


def _emit(line: str) -> None:
    """按控制台编码安全输出：基线键里有中文与非断行空格，GBK 码页会直接抛
    UnicodeEncodeError——那会让"报告新增类型错误"这件事本身把门禁脚本炸掉
    （2026-09-18 实测踩过），比错误更难查。
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(line.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="重新生成基线")
    args = parser.parse_args()

    current = current_errors()
    if args.update:
        BASELINE.write_text("\n".join(current) + ("\n" if current else ""), encoding="utf-8")
        print(f"baseline updated: {len(current)} errors frozen")
        return 0

    baseline = (
        [line for line in BASELINE.read_text(encoding="utf-8").splitlines() if line.strip()]
        if BASELINE.exists()
        else []
    )
    new = [key for key in current if key not in set(baseline)]
    _emit(f"pyright: {len(current)} errors (baseline {len(baseline)})")
    if new:
        _emit(f"\nNEW type errors not in baseline ({len(new)}):")
        for key in new:
            _emit(f"  {key}")
        _emit("\nfix them; baseline only shrinks (INV-1)")
        return 1
    if len(current) < len(baseline):
        # Ratchet closes behind you (INV-1: gates only tighten). A surplus left in the
        # baseline is debt the next person can silently reintroduce. Shrink it here.
        print(
            f"\nBASELINE IS STALE: {len(baseline) - len(current)} frozen errors are already gone.\n"
            "run `uv run python scripts/typecheck.py --update` and commit "
            "pyright-baseline.txt with your change."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
