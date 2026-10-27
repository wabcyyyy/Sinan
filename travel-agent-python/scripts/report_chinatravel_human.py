"""Aggregate the frozen Human run; no generation, scoring rerun or producer mutation."""

import csv
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal

from scripts.chinatravel_human import OUTPUT, PHASE, ROOT, SOURCE
from scripts.chinatravel_original_data import queries, save


def read(path):
    return json.loads(path.read_text("utf8"))


def percentile(values, fraction):
    return round(sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))], 3) if values else None


def stage(result, score):
    if any(issue.startswith("intake:") for issue in result["issues"]):
        return "intake_failed"
    if not result.get("business_plans") or not result["plan"]:
        return "no_shared_final"
    for name in ("schema", "common", "logical"):
        if not score[name]:
            return name + "_failed"
    return "runtime_gate_failed" if result["issues"] else "accepted_all_pass"


def report():
    protocol = read(OUTPUT / "protocol.json")
    closed = read(OUTPUT / "production-closed.json")
    if closed["completed"] != 154 or closed["scored"] != 154 or not closed["producer_hashes_match"]:
        raise ValueError("all154 production/scoring and unchanged frozen producer are required")
    scores = {}
    for path in sorted((OUTPUT / f"{PHASE}/score-batches").glob("*.json")):
        for row in read(path):
            key = row["uid"], row["arm"]
            if key in scores:
                raise ValueError("duplicate official score row")
            scores[key] = row
    expected = {(uid, arm) for uid in protocol["all"] for arm in "ABC"}
    if set(scores) != expected:
        raise ValueError("official first-pass denominator must remain154 per arm")
    results = {key: read(OUTPUT / f"{PHASE}/results/{key[0]}-{key[1]}.json") for key in scores}
    # The shared-final submission boundary was fixed before this run, never use failed candidates.
    for key, result in results.items():
        submitted = read(OUTPUT / f"official/results/SinanOriginal-{PHASE}-{key[1]}/{key[0]}.json")
        if result["plan"] != submitted or bool(result["plan"]) != bool(result.get("business_plans")):
            raise ValueError("submitted representation differs from the shared final boundary")
    snap = read(OUTPUT / "rolling-snapshot.json")
    summary = {
        "scope": snap["groups"],
        "stages": {},
        "issue_frequencies": {},
        "resources": {},
        "paired_B_C": {},
        "official_failed_columns": {},
        "failures": [],
        "closed": closed,
        "seven_day_proof": "week7-proof.json",
        "human_review": "not performed",
        "unseen_scope": "150 not registered as previously seen; not a template-family holdout",
        "latency_scope": "generation/check duration excluding shared intake/research; not online product latency",
        "cost_scope": "C has no generation model call but shares paid NL extraction; usage valued at peak rates",
    }
    cp_failures = []
    for (uid, arm), result in results.items():
        if arm != "C" or result["plan"]:
            continue
        traces = result.get("solver_trace", [])
        statuses = sorted({r["status"] for trace in traces for r in trace.get("results", [])})
        category = (
            "intake"
            if any(i.startswith("intake:") for i in result["issues"])
            else "runtime_error"
            if any(i.startswith("TypeError:") for i in result["issues"])
            else "bounded_search_UNKNOWN"
            if "UNKNOWN" in statuses
            else "scoped_INFEASIBLE"
            if statuses == ["INFEASIBLE"]
            else "timetable_or_common_or_setup"
        )
        cp_failures.append({"uid": uid, "category": category, "statuses": statuses, "issues": result["issues"]})
    summary["cp_no_final_breakdown"] = dict(Counter(r["category"] for r in cp_failures))
    summary["cp_no_final_cases"] = cp_failures
    for arm in "ABC":
        rows = [(result, scores[key]) for key, result in results.items() if key[1] == arm]
        summary["stages"][arm] = dict(Counter(stage(r, s) for r, s in rows))
        summary["issue_frequencies"][arm] = dict(Counter(issue for r, _ in rows for issue in r["issues"]))
        elapsed = [
            r["elapsed_s"] for r, _ in rows if r.get("elapsed_s", 0) > 0 and (arm != "B" or r.get("new_calls", 0))
        ]
        # B copied-A entries are not fresh generation latency samples.
        fresh = [r["elapsed_s"] for r, _ in rows if "elapsed_s" in r and r.get("new_calls", 0)]
        summary["resources"][arm] = {
            "stored_elapsed_samples": len(elapsed),
            "p50_s": percentile(elapsed, 0.5),
            "p95_s": percentile(elapsed, 0.95),
            "fresh_model_p50_s": percentile(fresh, 0.5),
            "new_generation_calls": sum(r.get("new_calls", 0) for r, _ in rows),
            "B_reused_A_without_repair": sum(
                bool(r.get("shared_A_first_output") and not r.get("new_calls")) for r, _ in rows
            )
            if arm == "B"
            else 0,
        }
        for result, score in rows:
            if stage(result, score) != "accepted_all_pass":
                summary["failures"].append(
                    {
                        "uid": result["uid"],
                        "arm": arm,
                        "stage": stage(result, score),
                        "issues": result["issues"],
                        "official": {k: score[k] for k in ("schema", "common", "logical")},
                        "source": f"{PHASE}/results/{result['uid']}-{arm}.json",
                    }
                )
    for name, ids in (
        ("all154", protocol["all"]),
        ("seen4", protocol["seen"]),
        ("remaining150", protocol["unseen_recorded"]),
    ):
        summary["paired_B_C"][name] = dict(
            Counter(
                "both_pass"
                if scores[uid, "B"]["all_pass"] and scores[uid, "C"]["all_pass"]
                else "B_only"
                if scores[uid, "B"]["all_pass"]
                else "C_only"
                if scores[uid, "C"]["all_pass"]
                else "both_fail"
                for uid in ids
            )
        )
    for arm in "ABC":
        columns = Counter()
        delivered_columns = Counter()
        for folder in {OUTPUT / r["official_csv_folder"] for r in scores.values() if r["arm"] == arm}:
            for name, pass_value in (("commonsense", "0"), ("logical_py", "1")):
                with (folder / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
                    for row in csv.DictReader(stream):
                        for key, value in row.items():
                            if key != "data_id" and value and Decimal(value) != Decimal(pass_value):
                                columns[f"{name}:{key}"] += 1
                                if results[row["data_id"], arm].get("business_plans"):
                                    delivered_columns[f"{name}:{key}"] += 1
        summary["official_failed_columns"][arm] = dict(columns)
        summary.setdefault("delivered_failed_columns", {})[arm] = dict(delivered_columns)
    # Oracle access occurs only after the frozen production has closed, for failure interpretation.
    by_uid = {r["uid"]: r for r in queries("human")}
    logical_cases = []
    for (uid, arm), score in scores.items():
        if arm != "C" or not results[uid, arm]["plan"] or score["logical"]:
            continue
        with (OUTPUT / score["official_csv_folder"] / "logical_py.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            row = next(row for row in csv.DictReader(stream) if row["data_id"] == uid)
        logical_cases.append(
            {
                "uid": uid,
                "original_text": by_uid[uid]["nature_language"],
                "failed_official_DSL": [
                    by_uid[uid]["hard_logic_py"][int(key.rsplit("_", 1)[1])]
                    for key, value in row.items()
                    if key != "data_id" and value and Decimal(value) != 1
                ],
                "runtime_issues": results[uid, arm]["issues"],
            }
        )
    save(
        OUTPUT / "posthoc-logical-failures.json",
        {
            "scope": "post-production diagnostic only; not producer input or new tuning",
            "C": logical_cases,
        },
    )
    ledger = read(SOURCE / "budget.json")
    historical = ledger["active_authorization"]
    records = ledger["records"]
    human = [r for r in records if r["id"].startswith(PHASE + "-")]

    def money(rows):
        return sum(Decimal(r.get("settled_peak_cny", r["reserved_cny"])) for r in rows)

    for arm in "ABC":
        calls = [r for r in human if f"-{arm}-" in r["id"]]
        summary["resources"][arm].update(
            request_records=len(calls),
            known_request_records=sum(r["status"] == "usage_known" for r in calls),
            peak_upper_cny=str(money(calls)),
            prompt_tokens=sum(r.get("usage", {}).get("prompt_tokens", 0) for r in calls),
            completion_tokens=sum(r.get("usage", {}).get("completion_tokens", 0) for r in calls),
        )

    summary["budget"] = {
        "cap_cny": ledger["cap_cny"],
        "cap_mode": ledger["cap_mode"],
        "prior_including_unknown_cny": historical["prior_peak_upper_cny"],
        "total_peak_upper_cny": ledger["total_peak_upper_cny"],
        "known_peak_cny": str(sum(Decimal(r["settled_peak_cny"]) for r in records if r["status"] == "usage_known")),
        "unknown_reserved_cny": str(sum(Decimal(r["reserved_cny"]) for r in records if r["status"] != "usage_known")),
        "in_flight": sum(r.get("request_state") == "in_flight" for r in records),
        "human_calls": len(human),
        "human_peak_upper_cny": str(money(human)),
        "human_known_calls": sum(r["status"] == "usage_known" for r in human),
        "human_prompt_tokens": sum(r.get("usage", {}).get("prompt_tokens", 0) for r in human),
        "human_completion_tokens": sum(r.get("usage", {}).get("completion_tokens", 0) for r in human),
        "shared_intake_peak_upper_cny": str(money([r for r in human if "-intake-" in r["id"]])),
    }
    summary["production_wall_seconds"] = round(
        (datetime.fromisoformat(closed["closed_utc"]) - datetime.fromisoformat(protocol["frozen_utc"])).total_seconds(),
        2,
    )
    save(OUTPUT / "summary.json", summary)
    lines = [
        "# Sinan Human154与七日共享能力交付（2026-10-26）",
        "",
        "正式首轮完整154题、A/B/C各154，4个独立UID进程；Gold仅供独立官方评分。",
        "不是实际旅行/在线用户采纳；七日真实模型NL加冻结WorldEnv业务证明单列。",
        "",
        "| 范围/臂 | 分母 | Schema | Common | Logical | All-pass | 业务门禁且All-pass |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for group, arms in summary["scope"].items():
        for arm, row in arms.items():
            lines.append(
                f"|{group}/{arm}|{row['planned']}|{row['schema']}|{row['common']}|{row['logical']}|"
                f"{row['all_pass']}|{row['business_gate_all_pass']}|"
            )
    lines += [
        "",
        "已见4题与其余150题按事先UID登记分报，不称全部未见或模板族外泛化。",
        "失败阶段、逐题异常、配对结果、Token与费用估计见summary.json；原失败/内部候选均保留。",
        "",
        "## 实验结论与负结果",
        "",
        "A为一次完整生成，B共享A首轮且最多一次普通反馈修复，C复用同一NL解析与公开事实、经共享CP规划。"
        "四类官方分数分别统计，不能把Logical单项或有内部候选当完整交付。",
        "C官方all-pass为92/154（59.74%），B为14/154（9.09%），本次冻结全题集差50.65个百分点；"
        "同时满足业务门禁分别72/154（46.75%）与5/154（3.25%）。"
        "B/C配对：共同通过10、仅C通过82、仅B通过4、共同失败58，不能宣称C逐题占优。",
        "长行程4—6天共59题，C通过27/59、B通过2/59；其中唯一6天题C也失败。"
        "7天能力来自另列匿名产品证明，不能将Human改称包含7天或全类型皆能交付。",
        "",
        "C的154题按互斥阶段划分：4题NL输入未完成、47题无共享最终对象、1题Common失败、"
        "10题其余Logical失败、20题官方全过但业务门禁仍阻断、72题共同通过。"
        "无最终对象的47题中，30题在已测有限候选/班次内被证明无解、13题限时UNKNOWN、4题空票价运行错误。"
        "30题并不代表整座城市或全部班次无解，13题不能写成不可行。",
        "20题官方全过而业务阻断，主要包含“最省钱/人少/悠闲”等仍未有明确可验证映射的原话；"
        "保留未解决意图，未删除条件换取可交付率。逐题原话、issues与判定路径见summary.json。",
        "",
        "A/B主要仍受时间/路由一致性影响：已交付对象中，官方时间顺序失败分别140/95题、"
        "景点闭馆失败55/35题（同题可多项失败）；空提交导致的整列失败未混算为这些机制归因。"
        "C已产生103份共享最终对象，其中102份Common通过；唯一Common失败伴随人数前缀4与"
        "“我和4个同学”语义冲突，模型解析为5人，官方餐饮成本仍按原标注4人核验。",
        "",
        "首轮关闭后才读取Gold解释C的11题Logical失败：6题涉及官方taxi=1辆与产品每4人1车的容量口径，"
        "其中1题另有人数冲突；其余涉及酒店每人每晚子预算、步行要求、具体海洋馆/动物园实体，"
        "以及原话未出现的川菜要求、原话“或者”而Gold要求三类景点全部出现。"
        "原分数全部保留，不剔除或修Gold；posthoc-logical-failures.json保留逐题原话/DSL对照。",
        "4题运行错误均在冻结公开班次中检出1条Cost=null，当前班次组合排序直接相加触发TypeError；"
        "posthoc-null-fares.json保存源记录。未把空票价改成0，未在冻结轮中修复或追分。",
        "",
        "## 耗时与调用",
        "",
        f"四UID并发整批墙钟{summary['production_wall_seconds']}秒（约14.97分钟）；"
        "各臂耗时为本地生成/检查阶段，排除共享NL和研究准备，不是在线API端到端延迟。",
        "| 臂 | 新生成调用 | 阶段P50秒 | 阶段P95秒 | 生成费用上界元 |",
        "|---|---:|---:|---:|---:|",
        *[
            f"|{arm}|{r['new_generation_calls']}|{r['p50_s']}|{r['p95_s']}|{r['peak_upper_cny']}|"
            for arm, r in summary["resources"].items()
        ],
        f"共享NL另有{summary['budget']['human_calls'] - 299}条请求记录；"
        f"Human共{summary['budget']['human_calls']}条，{summary['budget']['human_known_calls']}条有usage，"
        "3条用完整预占上界保留，未重试。B有1题直接复用A，不算新生成。"
        f"已知输入/输出Token={summary['budget']['human_prompt_tokens']}/"
        f"{summary['budget']['human_completion_tokens']}。C不额外调用生成模型，但共享付费NL，不能称全链免费。",
        "",
        "## 费用",
        "",
        f"累计高峰价上界{summary['budget']['total_peak_upper_cny']}元；"
        f"已知{summary['budget']['known_peak_cny']}元，未知预留{summary['budget']['unknown_reserved_cny']}元，"
        f"在途{summary['budget']['in_flight']}。Human新增{summary['budget']['human_peak_upper_cny']}元上界，"
        "用户已明确取消累计限额，原账本cap=null，不清零旧未知。",
        "已知usage按输入2/输出8元每百万Token高峰价保守折算，忽略缓存优惠；未知仍按预占。"
        "这是可核对的费用上界账本，未查询供应商实际扣款账单。",
        "",
        "## 七日能力与边界",
        "",
        "共享编译/求解/API/前端/受控重排日号支持1—7天，有限候选和全程求解预算；"
        "独立参考覆盖1/3/4/5/6/7天、精确预算、不可行、预约、先后与跨日锁定。",
        "匿名真实NL证明：七天六晚、两人、一间双床房、末日火车返回；实际API/worker函数/CP/"
        "SQLite及刷新一致，11244.45元。同步测试调度器、冻结官方WorldEnv，不是线上事实或实地收益。",
        "中间日必须有活动/餐饮，首末边界日可仅交通；未规定每天三餐或每天景点，"
        "住宿缺源容量沿用双人房规划假设，不声称最短入住小时已验。",
        "重排仍最多20条既有安排；先后关系限指定日内，跨日冻结/预约另行约束。",
        "",
        "## 验证与材料",
        "",
        "87项相关后端回归、5项业务/并发账本检查及前端定向检查通过，单批均90秒内；"
        "178项其余候选deferred to CI。契约已重导出；未跑全量CI，未伪造人审。",
        "唯一进度：docs/design/sinan-chinatravel-human-progress-20261026.md。",
        "产物：docs/benchmarks/sinan-chinatravel-human-20261026/；protocol.json、"
        "frozen-producer-source.zip、summary.json、week7-proof.json、human-v1/results/、"
        "human-v1/score-batches/、official/eval_res/。",
        "未提交、推送、部署，未修改正式简历。",
        "",
        "## 下一步及主张边界",
        "",
        "本轮指定的七日能力与Human154首轮评测已完成，剩余失败没有被标成通过。"
        "下一步优先在独立修复阶段处理Cost=null边界与人数冲突协商、酒店子预算/实体绑定，"
        "用本轮已见失败作标注回归，保留首轮分数；不自动重跑Human追分。"
        "再评估有限候选覆盖和UNKNOWN耗时，性能与召回分开比较。",
        "可供总控选用：①将共享CP/API/前端范围由1—3天扩至1—7天，补齐全程预算与跨日连接，"
        "匿名真实NL七天六晚经worker/SQLite刷新验证。②在ChinaTravel Human154冻结首轮中，"
        "CP臂官方all-pass92/154，对照一次生成2/154、一次反馈修复14/154；"
        "业务门禁交集72/154，范围为冻结公开环境而非实地旅行。"
        "③以4个隔离进程、单原账本和失败不出分母执行618条真实模型请求，原始请求/响应/费用/官方CSV可追溯。",
        "能力条目仍需本人核读才能作为本人能讲清的主张；未发生人审、未完成全量CI保持未审/未跑，"
        "不把这些前置成继续技术交付的阻塞。",
    ]
    (ROOT / "docs/design/sinan-chinatravel-human-delivery-20261026.md").write_text(
        "\n".join(lines) + "\n", encoding="utf8"
    )
    print(json.dumps({"all": summary["scope"]["all"], "budget": summary["budget"]}, ensure_ascii=False))


if __name__ == "__main__":
    report()
