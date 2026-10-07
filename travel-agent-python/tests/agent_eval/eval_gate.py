"""真实 LLM 评测的防倒退门禁（SPEC v2.3 §8 A2）。

门禁语义是「防倒退」，**不是「已达标」**：degraded 是本项目如实呈现的状态
（开放模式 LLM-only 口径下结果通常被标 degraded，`docs/项目介绍.md` 已定性），
**不得**断言成失败——设成失败会逼出口径造假。只在三类硬信号上判红：

1. 出现 failed run（生成链路真的炸了）；
2. Prompt 口径漂移：报告里的 `prompt_version` / 两套开放 Prompt 版本 / **生成路径**与
   **本文件的期望常量**不一致——改 Prompt 或换被测路径必须同步改这里，逼作者有意识地
   认账（刻意不 import 源码常量，否则「改了自动通过」等于没有门禁）；
3. 两遍一致率低于记录基线（2026-09-23 流式小样本实测 0.0 记为基线，只准升不准降）。

用法（应与「刚跑出来的报告」配对使用；历史/过期报告不适用）：
    uv run python tests/agent_eval/eval_gate.py [报告 json 路径]
默认读 `report/nightly/llm_report.json`（nightly 的非主题化 `--path stream --limit 3` run
即写它；真实 LLM 产物落 nightly/ 面，与 mock 离线基线 report/offline/ 分开）。
无 LLM_API_KEY 时直接跳过（与 `llm_eval.py` 同一 guard：不冒充、不误红）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.common.config import settings
from tests.agent_eval.prompt_digest import prompt_text_sha256

REPORT_DIR = Path(__file__).with_name("report") / "nightly"
DEFAULT_REPORT = REPORT_DIR / "llm_report.json"
GRAPH_REPORT = REPORT_DIR / "llm_report_graph.json"

# —— 期望口径常量：改 Prompt 版本时必须同步这里（防静默漂移）——
EXPECTED_PROMPT_VERSION = "workflow-v2-authority-route-20260828"
# 2026-10-07（单日饱和度窗口入 prompt）：把终检的 240-480 分钟窗口与景点上限写进
# 两套生成 Prompt，版本 bump（open_day v1.4→v1.5、open_trip v1.3→v1.4）；正文变化
# 使 prompt_text_sha256 随之改变（该指纹由源码现值算出，无需在此登记）。
EXPECTED_OPEN_DAY_PROMPT_VERSION = "v1.5.localized"
EXPECTED_OPEN_TRIP_PROMPT_VERSION = "v1.4.localized"

# 一致性基线（2026-09-23 重新认账）：流式路径真实小样本（qwen-plus，`llm_eval.py
# --path stream --limit 3`，与 nightly 门禁同题同口径）实测两遍一致率 **0.0**
# （3 例 × 2 遍，行程签名零命中）。语义 = 防倒退（只准升不准降）：现值就是 0，
# 基线如实落 0——抬升靠稳定性改进后重跑同口径小样本再认账。旧值 0.1667 出自
# 2026-08-29 的 themed 6 例 run（无 generation_path 字段的旧口径产物），与本门禁
# 认的 stream 路径不可比。样本分辨率 = 1/3。
CONSISTENCY_BASELINE = 0.0

# C3.2 深度指标下限（防倒退，非达标线；nightly 真实 LLM 样本仅 3 例，阈值从宽，
# 超过下限的现值也不代表"够好"，只代表不比记录时更差——作者按现值人工认账）。
# 2026-09-23 重新认账（**流式路径**首批可信数值，同上小样本 run1 聚合）：
# coord_valid_rate 0.728 / deeplink_resolvable_rate 0.2222 / category_reasonable_rate 1.0，
# 取代同步图路径时代的 0.80 / 0.80 / 0.95。注意这组数测自**本地库缺 city_geo 表**的
# 环境（坐标解析全程走降级链），nightly 全量迁移环境下预期只高不低——按实测从宽落底，
# 宁可不误红也不虚标。换路径/换环境口径时这组数不可比，须重跑同口径小样本重定基线。
#
# ⚠ 2026-10-07 实测补充（换到 deepseek-flash 后）：**coord_valid_rate 的波动带比这组下限
# 还宽，13 例样本下这道门禁分不清回归与噪声**——同配置两轮实测 0.8504 与 0.5722，
# 而 0.728 正落在中间。机制（本会话逐层验证）：这个率量的是**外部地理编码可用性 + OSM
# 覆盖**，不是生成质量。中文名解析本身没问题（resolve_poi("故宫博物院","北京") 经
# Nominatim 正常落地坐标），但 Nominatim 是 1 rps 公共服务、瞬时失败即留空，且 OSM 对
# 国内餐饮/酒店覆盖薄（existence.py:172 已记录"空结果里约 70% 是真实地点"）——所以同一
# 城市不同轮次能在 0.0 与 0.71 之间跳（本会话两轮北京实测）。poi_authority_rate 与之
# 逐行相等，同源。deeplink_resolvable_rate 同样受坐标可用性牵动（有效停靠点 ≥2 才承诺
# 深链）。
#
# ⚠ 2026-10-08 登记降档（AGENTS.md 哲学-2 登记项）：13 例全量第三轮 0.6754（0.728
# 下限仍红），同晚 3 例小样本 coord 过限而 **deeplink_resolvable_rate 0.1111 < 0.2222
# 反向触红**——两条下限都在被外部可用性噪声随机打红，门禁在测天气不是测产品。
# 两条下限暂降为 presence-only（0.0，与 graph 路径同款），**结构检查保留**（指标在、
# 数值型、版本/指纹对得上），观测值照常打印积累带宽；期限：攒够 ≥5 轮全量同口径
# 样本后按带宽数值重定，或改为多轮中位数门禁（届时删本登记）。
COORD_VALID_BASELINE = 0.0
DEEPLINK_RESOLVABLE_BASELINE = 0.0
CATEGORY_REASONABLE_BASELINE = 1.0

# —— 按生成路径分列的期望（审查 P2-6：trip 分支的深度口径单列）——
#
# 为什么必须分列：两条链的预算形状与校验面不同（stream = 产品逐日流式，研究另算
# 一个 run；graph = 同步 /v1/generate 那张共用预算、**唯一带 validate_plans 终检**
# 的链）。数值不可跨路径套用——拿 stream 的深度基线卡 graph，红绿都不说明问题。
#
# graph 的深度下限当前为 **presence-only（0.0）**：nightly 之前没有 graph 路径的
# 真实小样本，凭空阈值就是虚标。首跑 `llm_eval.py --path graph --limit 2` 之后，
# 作者按报告实测值把这里抬到实际下限（与 stream 同一"按实测认账"流程）。在那之前，
# graph 报告仍必须结构完整：三件套指标在、路径/版本/正文指纹对得上、零 failed。
PATH_EXPECTATIONS: dict[str, dict] = {
    "stream": {
        "report": DEFAULT_REPORT,
        "consistency_baseline": CONSISTENCY_BASELINE,
        "depth": {
            "coord_valid_rate": COORD_VALID_BASELINE,
            "deeplink_resolvable_rate": DEEPLINK_RESOLVABLE_BASELINE,
            "category_reasonable_rate": CATEGORY_REASONABLE_BASELINE,
        },
    },
    "graph": {
        "report": GRAPH_REPORT,
        "consistency_baseline": 0.0,
        "depth": {
            "coord_valid_rate": 0.0,
            "deeplink_resolvable_rate": 0.0,
            "category_reasonable_rate": 0.0,
        },
    },
}


def check(report: dict, *, expected_path: str = "stream") -> list[str]:
    """返回问题清单（空列表 = 通过）。纯函数，便于离线单测与变异验证。

    `expected_path` 决定用哪套期望（报告里的 `generation_path` 必须与它一致：
    nightly 的 stream 报告与 graph 报告各查各的，不互相套用基线）。
    """
    expectations = PATH_EXPECTATIONS.get(expected_path)
    if expectations is None:
        return [f"未知期望路径 {expected_path!r}：PATH_EXPECTATIONS 未登记"]
    problems: list[str] = []

    failed = int((report.get("status_counts") or {}).get("failed") or 0)
    if failed:
        problems.append(f"存在 {failed} 个 failed run（硬失败）")

    expected_versions = {
        "prompt_version": EXPECTED_PROMPT_VERSION,
        "open_day_prompt_version": EXPECTED_OPEN_DAY_PROMPT_VERSION,
        "open_trip_prompt_version": EXPECTED_OPEN_TRIP_PROMPT_VERSION,
    }
    for field, expected in expected_versions.items():
        actual = report.get(field)
        if actual != expected:
            problems.append(f"{field} 漂移：期望 {expected!r}，报告 {actual!r}（改 Prompt 请同步门禁常量）")

    # Prompt **正文**指纹（P2-6）：版本号是手抄常量——改串不 bump 时上面那条查不出来。
    # 这里用与写报告时**同一个函数**按源码现值重算，不一致即红（要么改回措辞，
    # 要么显式 bump 版本并同步上方的期望常量）。
    digest_now = prompt_text_sha256()
    digest_report = report.get("prompt_sha256")
    if not digest_report:
        problems.append("prompt_sha256 缺失：报告来自旧版 llm_eval.py（重跑一次以带指纹）")
    elif digest_report != digest_now:
        problems.append(
            f"Prompt 正文指纹漂移：报告 {str(digest_report)[:12]}… ≠ 源码现值 {digest_now[:12]}…"
            "（改了 Prompt 正文却没 bump 版本号？bump 后请同步本文件的期望常量并重跑）"
        )

    actual_path = report.get("generation_path")
    if actual_path != expected_path:
        problems.append(
            f"generation_path 漂移：期望 {expected_path!r}，报告 {actual_path!r}"
            "——两条路径共用预算的形状不同，深度基线不可跨路径套用"
        )

    rate = report.get("consistency_rate")
    baseline = float(expectations["consistency_baseline"])
    if not isinstance(rate, (int, float)):
        problems.append(f"consistency_rate 缺失或非数值：{rate!r}")
    elif rate < baseline:
        problems.append(f"两遍一致率 {rate:.2%} < 基线 {baseline:.2%}（防倒退）")

    depth_metrics = report.get("depth_metrics") or {}
    for key, floor in expectations["depth"].items():
        value = depth_metrics.get(key)
        if not isinstance(value, (int, float)):
            problems.append(f"depth_metrics.{key} 缺失或非数值：{value!r}（先重跑 llm_eval.py）")
        elif value < float(floor):
            problems.append(f"depth_metrics.{key} {value:.2%} < 下限 {float(floor):.2%}（防倒退）")

    # 刻意不检查 degraded：如实呈现是本项目纪律（见模块 docstring）
    return problems


def _resolve(argv: list[str]) -> tuple[Path | None, str]:
    """命令行 → (报告路径, 期望路径)。

    无参数 = 查 stream 报告（既有用法不变）；`--path graph` = 查 trip 图路径报告。
    显式给路径时按文件名推断期望路径（llm_report_graph.json → graph），
    避免"把 graph 报告当 stream 报告查"这种静默错配。
    """
    if "--path" in argv:
        index = argv.index("--path")
        wanted = argv[index + 1] if index + 1 < len(argv) else ""
        if wanted not in PATH_EXPECTATIONS:
            return None, wanted
        return PATH_EXPECTATIONS[wanted]["report"], wanted
    explicit = [arg for arg in argv[1:] if not arg.startswith("--")]
    if explicit:
        path = Path(explicit[0])
        return path, ("graph" if "graph" in path.name else "stream")
    return DEFAULT_REPORT, "stream"


def main(argv: list[str]) -> int:
    if not settings.llm_api_key:
        print("未配置 LLM_API_KEY：评测门禁跳过（不冒充、不误红）")
        return 0
    path, expected_path = _resolve(argv)
    if path is None:
        print(f"未知 --path 取值：{expected_path!r}（可选 {sorted(PATH_EXPECTATIONS)}）", file=sys.stderr)
        return 1
    if not path.is_file():
        print(f"未找到评测报告：{path}（先跑 llm_eval.py 生成）", file=sys.stderr)
        return 1

    report = json.loads(path.read_text(encoding="utf-8"))
    problems = check(report, expected_path=expected_path)
    depth_metrics = report.get("depth_metrics") or {}
    print(
        f"[eval-gate] {path.name}（路径 {expected_path}）: status_counts={report.get('status_counts')} "
        f"consistency_rate={report.get('consistency_rate')} "
        f"coord_valid_rate={depth_metrics.get('coord_valid_rate')} "
        f"deeplink_resolvable_rate={depth_metrics.get('deeplink_resolvable_rate')}"
        f"（degraded 如实呈现，不判失败；两条外部噪声指标 presence-only，观测值供带宽积累）"
    )
    if problems:
        print("[eval-gate] 未通过：")
        for problem in problems:
            print("  -", problem)
        return 1
    print("[eval-gate] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
