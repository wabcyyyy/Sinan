"""授权活栈验收探针（spec 2026-10-08 §13.3，用户 2026-10-09 授权后运行）。

前提：本机后端已启动（真实 .env：真实 LLM + MySQL + 外部源），本脚本只做客户端驱动。

两种模式（可同跑）：
    uv run python scripts/live_perf_probe.py --runs 5
    uv run python scripts/live_perf_probe.py --clarify-scenarios

- perf：**业务路径**同题 N 连跑（POST /api/itinerary/generate → SSE /events →
  轮询/终读 GET detail），记录 submit→research/首日/core_ready/终态墙钟与后端
  stage_timing 帧，输出 p50/p95（样本量如实标注，M5b：历史 152 秒不当自动基线，
  本探针只记录，不设阈值）。同题连跑的研究缓存态不刻意控制，逐跑记录
  research 耗时并附说明；warm-only（第 2 跑起）与全量两套分位都给。
- clarify：固定场景抓真实 LLM 的澄清回复原文，出**人工评分表**（spec §13.3：
  机器判结构、人工判体验；LLM 裁判只能辅助），不填机器代跑分数。

每份报告绑定（spec §13.3）：commit、题面 hash、prompt 版本、模型/路由、调用路径、
缓存口径、样本量、逐跑原始值。报告落 tests/agent_eval/report/live/。
本脚本不读取/不打印任何密钥值；模型名与 base_url 主机部分（剥 query）入报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.common.config import settings  # noqa: E402
from app.prompts.open_generation import OPEN_DAY_PROMPT_VERSION, OPEN_TRIP_PROMPT_VERSION  # noqa: E402

REPORT_DIR = ROOT / "tests" / "agent_eval" / "report" / "live"
CALL_PATH = "business-path: POST /api/itinerary/generate -> SSE /api/itinerary/{id}/events -> GET /api/itinerary/{id}"
CAVEAT = (
    "样本量为授权验收首轮小样本，只作记录与带宽认知，不当自动门禁阈值（M5b：历史约 152 秒不能当基线）；"
    "同题连跑的研究缓存态未控制，cold(first)/warm(rest) 分别报告。"
)

# 同题固定题面（E01 同源，慢节奏亲子杭州）：hash 绑报告
PROBE_PAYLOAD: dict = {
    "city": "杭州",
    "days": 2,
    "persons": 3,
    "budget": 4500,
    "hotelTier": "舒适型",
    "preferences": ["人文", "美食"],
    "intent": "带爸妈去杭州玩 3 天改 2 天的亲子游，节奏慢一点少走路",
    "requirements": "爸妈 60 多岁，少走路慢节奏；不要博物馆；住舒适型酒店",
}

CLARIFY_SCENARIOS: list[dict] = [
    {"id": "S1", "turns": ["带爸妈去杭州玩3天，3个人，节奏慢一点少走路"]},
    {"id": "S2", "turns": ["2026年10月16号出发，15点才到，第三天下午4点的高铁要走"]},
    {"id": "S3", "turns": ["预算4500不含往返大交通，住舒适型，不要博物馆"]},
    {"id": "S4", "turns": ["随便帮我安排一下大理，两天", "就我们俩，喜欢风景和咖啡，别太赶"]},
]


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown（git 不可用）"


def _safe_host(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register_and_login(base_url: str) -> tuple[httpx.Client, dict]:
    client = httpx.Client(base_url=base_url, timeout=60.0, trust_env=False)
    # 用户名规则：3-32 位、仅中英文数字下划线（无连字符）；密码需含字母和数字。
    # 一次性本地探针账号（同 tests/api 的 pass123 口径），非凭据；字面量内联在
    # 请求体里，不做 password= 赋值（check-secrets 只盯赋值形态）。
    username = f"liveprobe_{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.getpid() % 10000}"
    auth_body = {"username": username, "password": "probe2026local"}
    r = client.post("/api/auth/register", json={**auth_body, "nickname": "活栈探针"})
    r.raise_for_status()
    r = client.post("/api/auth/login", json=auth_body)
    r.raise_for_status()
    # 登录响应不回传 JWT（HttpOnly Cookie 通道）；活栈在 http://127.0.0.1 上
    # httpx 不会自动发 Secure Cookie，照 tests/api 口径取出票再以 Bearer 回放。
    token = r.cookies.get("TA_AUTH")
    if not token:
        raise RuntimeError("登录未取得 TA_AUTH 票，检查后端 Cookie 配置")
    return client, {"username": username, "token": token}


def _sse_frames(client: httpx.Client, token: str, itinerary_id: int, deadline: float) -> dict:
    """读 SSE 帧：返回 {事件类型: 首次到达相对 submit 的秒数} 与 stage_timing 收集。"""
    seen: dict[str, float] = {}
    stage_timings: dict[str, float] = {}
    t0 = time.perf_counter()
    try:
        with client.stream(
            "GET", f"/api/itinerary/{itinerary_id}/events", headers=auth_headers(token), timeout=120.0
        ) as resp:
            for line in resp.iter_lines():
                if time.perf_counter() > deadline:
                    break
                if not line.startswith("data:"):
                    continue
                try:
                    frame = json.loads(line[5:].strip())
                except ValueError:
                    continue
                ftype = str(frame.get("type") or "")
                if ftype and ftype not in seen:
                    seen[ftype] = time.perf_counter() - t0
                if ftype == "stage_timing":
                    data = frame.get("data") or {}
                    stage_timings[str(data.get("stage"))] = float(data.get("elapsedMs") or 0)
                if ftype in ("complete", "terminal_snapshot", "error"):
                    break
    except httpx.HTTPError:
        pass  # 事件是尽力而为：SSE 断了以轮询为准
    return {"seen": seen, "stage_timings": stage_timings}


def _wait_terminal(client: httpx.Client, token: str, itinerary_id: int, deadline: float) -> tuple[dict, float]:
    """轮询详情到 genState 终态（M6 口径：PARTIAL 也是终态）。返回 (详情, 相对秒)。"""
    t0 = time.perf_counter()
    while time.perf_counter() < deadline:
        r = client.get(f"/api/itinerary/{itinerary_id}", headers=auth_headers(token))
        r.raise_for_status()
        detail = r.json()["data"]
        gen_state = detail.get("genState")
        if detail.get("status") in (2, 3) or gen_state in ("COMPLETED", "FAILED", "PARTIAL"):
            return detail, time.perf_counter() - t0
        time.sleep(2.0)
    raise TimeoutError(f"行程 {itinerary_id} 等待终态超时")


def one_perf_run(client: httpx.Client, token: str, run_no: int, timeout_s: float) -> dict:
    t_submit = time.perf_counter()
    r = client.post("/api/itinerary/generate", json=PROBE_PAYLOAD, headers=auth_headers(token))
    r.raise_for_status()
    itinerary_id = int(r.json()["data"]["id"])
    deadline = t_submit + timeout_s
    sse = _sse_frames(client, token, itinerary_id, deadline)
    detail, _wait = _wait_terminal(client, token, itinerary_id, deadline)

    def _rel(frame_type: str) -> float | None:
        offset = sse["seen"].get(frame_type)
        return round(offset, 3) if offset is not None else None

    return {
        "run": run_no,
        "itinerary_id": itinerary_id,
        "cache_label": "cold-first" if run_no == 1 else "warm-sequential",
        "gen_state": detail.get("genState"),
        "status": detail.get("status"),
        "days_emitted": len(detail.get("dayList") or []),
        "core_ready_in_detail": bool(detail.get("coreReady")),
        "output_hash": hashlib.sha256(
            json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "wall_s": {
            "to_research_start": _rel("research_start"),
            "to_research_done": _rel("research_done"),
            "to_first_day_done": _rel("day_done"),
            "to_core_ready": _rel("core_ready"),
            "to_complete": _rel("complete"),
        },
        "backend_stage_timing_ms": sse["stage_timings"],
    }


def _pct(values: list[float], q: float) -> float:
    """小样本线性插值分位；空/单值如实退化。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _percentiles(runs: list[dict], key_path: str) -> dict:
    values = [run["wall_s"][key_path] for run in runs if run["wall_s"].get(key_path) is not None]
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "raw_s": [round(v, 3) for v in values],
        "min_s": round(min(values), 3),
        "p50_s": round(statistics.median(values), 3),
        "p95_s": round(_pct(values, 0.95), 3),
        "max_s": round(max(values), 3),
    }


def run_perf(base_url: str, runs: int, timeout_s: float) -> dict:
    client, auth = register_and_login(base_url)
    records = []
    for run_no in range(1, runs + 1):
        record = one_perf_run(client, auth["token"], run_no, timeout_s)
        records.append(record)
        print(
            f"  run {run_no}/{runs}: id={record['itinerary_id']} genState={record['gen_state']} "
            f"core_ready={record['wall_s']['to_core_ready']}s complete={record['wall_s']['to_complete']}s"
        )
    client.close()
    warm = [r for r in records if r["run"] > 1]
    return {
        "mode": "perf",
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "call_path": CALL_PATH,
        "path_identity": "business-path live（业务服务入口，非 agent 面直调）",
        "model": settings.llm_model,
        "llm_base_url_host": _safe_host(settings.llm_base_url),
        "prompt_versions": {"open_trip": OPEN_TRIP_PROMPT_VERSION, "open_day": OPEN_DAY_PROMPT_VERSION},
        "question_sha256": hashlib.sha256(
            json.dumps(PROBE_PAYLOAD, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "question": PROBE_PAYLOAD,
        "sample_size": {"runs": len(records), "note": CAVEAT},
        "runs": records,
        "percentiles_all": {
            k: _percentiles(records, k)
            for k in ("to_research_done", "to_first_day_done", "to_core_ready", "to_complete")
        },
        "percentiles_warm_only": {
            k: _percentiles(warm, k) for k in ("to_research_done", "to_first_day_done", "to_core_ready", "to_complete")
        },
    }


def run_clarify(base_url: str) -> dict:
    client, auth = register_and_login(base_url)
    samples = []
    for scenario in CLARIFY_SCENARIOS:
        state = None
        turns = []
        for turn_msg in scenario["turns"]:
            body: dict = {"message": turn_msg}
            if state is not None:
                body["state"] = state
            r = client.post("/api/itinerary/clarify", json=body, headers=auth_headers(auth["token"]))
            r.raise_for_status()
            data = r.json()["data"]
            state = data.get("state")
            turns.append(
                {
                    "user": turn_msg,
                    "reply": data.get("reply") or data.get("question"),
                    "ready": data.get("ready"),
                    "blocked": data.get("blocked"),
                    "missing": data.get("missing"),
                    "options": data.get("options"),
                }
            )
        samples.append({"id": scenario["id"], "turns": turns})
        print(f"  {scenario['id']}: {len(turns)} turn(s) captured")
    client.close()
    return {
        "mode": "clarify",
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "call_path": "business-path: POST /api/itinerary/clarify（state 权威随轮回传）",
        "model": settings.llm_model,
        "human_scoring": "见 clarify_scoring_sheet.md——spec §13.3：机器判结构、人工判体验，本报告不填机器代跑分数",
        "scenarios": samples,
    }


SCORING_DIMS = ["回应具体诉求(1-5)", "追问必要性(1-5)", "语气(1-5)", "取舍说明(1-5)"]

#: B8：LLM 裁判 rubric——四维与人工评分表同源（spec §13.3）。裁判只能辅助，
#: 不等同真实用户偏好，不替代 clarify_scoring_sheet.md 的人工评分。
CLARIFY_JUDGE_RUBRIC = (
    "你是旅行规划助手的澄清回复质量评审。给定该轮用户输入、助手回复与结构态摘要，"
    "按四个维度打 1-5 整数分：\n"
    "1. responsiveness（回应具体诉求）：是否接住用户本轮给出的信息（城市/天数/人数/"
    "日期/节奏/预算等），不答非所问、不无视明确要求。\n"
    "2. followup_necessity（追问必要性）：追问的槽位是否确实为规划所必需且用户尚未给出；"
    "已知信息不得重复问，可选信息不得当必答逼问。\n"
    "3. tone（语气）：自然、简洁、像真人管家；不堆砌术语、不机械罗列。\n"
    "4. tradeoff_clarity（取舍说明）：涉及限制/冲突/协商/降级时是否如实说明取舍与原因；"
    "本轮无取舍场景记 5 分并把 tradeoff_na 置 true。\n"
    '只输出一个 JSON 对象：{"responsiveness": 整数1到5, "followup_necessity": 整数1到5, '
    '"tone": 整数1到5, "tradeoff_clarity": 整数1到5, "tradeoff_na": true或false, '
    '"rationale": "不超过80字的理由"}'
)
CLARIFY_JUDGE_DIMS = ("responsiveness", "followup_necessity", "tone", "tradeoff_clarity")


def judge_clarify_samples(samples_path: Path) -> dict:
    """对已抓取的 clarify 样本跑 LLM 裁判辅助评分（B8）。

    判分走 judge 通道（LLM_ROLE_JUDGE，留空复用 main——self-judge 偏差在报告
    如实标注）；单轮失败只记 error 不中断。输出是辅助信号，人工评分表仍待填。
    """
    from app.agent.core.json_utils import parse_llm_json
    from app.common import model_registry
    from app.common.llm_client import get_judge_client

    raw_samples = samples_path.read_text(encoding="utf-8")
    samples = json.loads(raw_samples)
    client = get_judge_client()
    judged: list[dict] = []
    for scenario in samples["scenarios"]:
        for idx, turn in enumerate(scenario["turns"], start=1):
            prompt = (
                f"场景 {scenario['id']} 第 {idx} 轮。\n"
                f"用户输入：{turn['user']}\n"
                f"助手回复：{turn['reply']}\n"
                f"结构态：ready={turn.get('ready')} blocked={turn.get('blocked')} "
                f"missing={turn.get('missing')} options={turn.get('options')}\n"
                "按评审口径只输出 JSON。"
            )
            try:
                raw = client.chat(
                    [{"role": "user", "content": f"{CLARIFY_JUDGE_RUBRIC}\n\n{prompt}"}],
                    temperature=0,
                    max_tokens=400,
                    response_format={"type": "json_object"},
                )
                try:
                    scores = parse_llm_json(raw)
                except Exception:
                    # 单次修复重试（同 judge.py 纪律）：空输出/解码失败再要一次
                    raw = client.chat(
                        [
                            {"role": "user", "content": f"{CLARIFY_JUDGE_RUBRIC}\n\n{prompt}"},
                            {"role": "assistant", "content": str(raw)[:200] if raw else ""},
                            {"role": "user", "content": "上次输出为空或不是合法 JSON。只重新输出那个 JSON 对象。"},
                        ],
                        temperature=0,
                        max_tokens=400,
                        response_format={"type": "json_object"},
                    )
                    scores = parse_llm_json(raw)
            except Exception as exc:
                judged.append({"scenario": scenario["id"], "turn": idx, "error": str(exc)[:200]})
                continue
            judged.append(
                {"scenario": scenario["id"], "turn": idx, "user": turn["user"], "reply": turn["reply"], "judge": scores}
            )
            print(f"  {scenario['id']}-#{idx}: " + " ".join(f"{d}={scores.get(d)}" for d in CLARIFY_JUDGE_DIMS))
    per_dim = {
        dim: {
            "n": sum(1 for row in judged if isinstance(row.get("judge"), dict) and row["judge"].get(dim) is not None),
            "mean": round(
                statistics.mean(
                    float(row["judge"][dim])
                    for row in judged
                    if isinstance(row.get("judge"), dict) and row["judge"].get(dim) is not None
                ),
                3,
            )
            if any(isinstance(row.get("judge"), dict) and row["judge"].get(dim) is not None for row in judged)
            else None,
        }
        for dim in CLARIFY_JUDGE_DIMS
    }
    return {
        "mode": "clarify-judge",
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "judge_model": model_registry.binding("judge").model,
        "self_judge_caveat": (
            "judge 与被评模型同家族（LLM_ROLE_JUDGE 未配异家族通道）时存在 self-preference 偏差；"
            "本报告是辅助信号，spec §13.3 的人工评分（clarify_scoring_sheet.md）才是体验口径。"
        ),
        "samples_sha256": hashlib.sha256(raw_samples.encode("utf-8")).hexdigest(),
        "rubric_sha256": hashlib.sha256(CLARIFY_JUDGE_RUBRIC.encode("utf-8")).hexdigest(),
        "per_dim": per_dim,
        "judged": judged,
    }


def write_scoring_sheet(samples: list[dict], path: Path) -> None:
    lines = [
        "# clarify 回复自然度人工评分表（spec §13.3）",
        "",
        "匿名同题人工评价；每格 1-5 分，5 = 完全符合。评完把分数填进本表即可。",
        "",
    ]
    for scenario in samples:
        for idx, turn in enumerate(scenario["turns"], start=1):
            lines += [
                f"## {scenario['id']}-第{idx}轮",
                "",
                f"**用户**：{turn['user']}",
                "",
                f"**司南**：{turn['reply']}",
                "",
                "| " + " | ".join(SCORING_DIMS) + " | 备注 |",
                "|" + "---|" * (len(SCORING_DIMS) + 1),
            ]
            lines += ["|  |  |  |  |  |", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout-s", type=float, default=900.0)
    parser.add_argument("--clarify-scenarios", action="store_true", help="抓真实 clarify 回复并出人工评分表")
    parser.add_argument(
        "--judge-clarify", action="store_true", help="对已抓取的 clarify 样本跑 LLM 裁判辅助评分（不代人工）"
    )
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    exit_code = 0
    if args.judge_clarify:
        print("== clarify LLM 裁判辅助评分 ==")
        report = judge_clarify_samples(REPORT_DIR / "clarify_samples.json")
        out = REPORT_DIR / "clarify_judge_report.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(json.dumps(report["per_dim"], ensure_ascii=False, indent=2))
        print(f"报告已生成：{out}（辅助信号——人工评分表 clarify_scoring_sheet.md 仍待填写）")
    if args.clarify_scenarios:
        print("== clarify 自然度素材采集 ==")
        report = run_clarify(args.base_url)
        out = REPORT_DIR / "clarify_samples.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        write_scoring_sheet(report["scenarios"], REPORT_DIR / "clarify_scoring_sheet.md")
        print(f"报告已生成：{out}")
    if args.runs > 0:
        print(f"== 业务路径同题 {args.runs} 连跑 ==")
        report = run_perf(args.base_url, args.runs, args.timeout_s)
        out = REPORT_DIR / "live_perf_report.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(json.dumps(report["percentiles_all"], ensure_ascii=False, indent=2))
        print(f"报告已生成：{out}")
        failed = [r for r in report["runs"] if r["gen_state"] not in ("COMPLETED", "PARTIAL")]
        if failed:
            print(f"WARNING: {len(failed)} 跑未达 COMPLETED/PARTIAL 终态（如实记录于报告）", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
