"""IATA 映射离线校验快照（LA3）：用 Travelpayouts 免费数据集给 city_iata 短表背书。

背景：`app/agent/data/city_iata.py` 是纯人工短表，"这个代码对不对"此前只是人工
责任。本脚本把它升级为**人工断言 + 免费权威数据集校验**——不自动扩表：校验只认
已登记的行，未命中仍一律缺席（短表纪律不变）；校验不过的行不进快照，机检立刻红。

数据源（免 token 静态 JSON，与已有 token 通道无关；实测 2026-09-26 均 200）：
- https://api.travelpayouts.com/data/airports.json  机场字典（iata_type=airport 才算机场，
  该表还混有 railway/bus/heliport/harbour 条目，展开时必须过滤）；
- https://api.travelpayouts.com/data/cities.json    城市字典（TYO/MOW/LON 等**纯城市码**
  只在此表，airports.json 查无——这也是本脚本必须同时取两张表的原因）；
- https://api.travelpayouts.com/data/routes.json    航线表，出入都是**机场码**——城市码
  须先展开为该市机场集合（city_code 归组）再查双向航线覆盖。

用法（travel-agent-python 目录下；手工跑，**不进 just check**，CI 无外网是既有假设）：
    uv run python scripts/iata_snapshot.py                 # 全表校验 → 重写 tests/fixtures/city_iata_snapshot.json
    uv run python scripts/iata_snapshot.py --check 杭州=HGH 丽江=LJG
                                                           # 候选行核查：只出报告，不写文件、不改表——
                                                           # 新增一行仍是一次人工事实断言

失败语义：任一表行校验不过 → 打印拒绝原因、**不写快照**、退出码 1。快照永远只含
被数据集背书的条目；此时 tests/test_flight_chain.py 的存在性校验红 = 事实断言被
数据集否决，须人工复核，不自动改表（数据集只做背书不做真源——它自身可能停更）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

# 允许脚本独立运行（sys.path[0] 是 scripts/，需补模块根）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.data import city_iata
from scripts.probe_sources import USER_AGENT

AIRPORTS_URL = "https://api.travelpayouts.com/data/airports.json"
CITIES_URL = "https://api.travelpayouts.com/data/cities.json"
ROUTES_URL = "https://api.travelpayouts.com/data/routes.json"
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "city_iata_snapshot.json"

SNAPSHOT_NOTE = (
    "LA3 存在性校验快照：city_iata 表每行代码的数据集背书。由 scripts/iata_snapshot.py "
    "生成，勿手改；表与快照双向一致的机检见 tests/test_flight_chain.py。"
)


@dataclass(frozen=True)
class Evidence:
    """一个代码的数据集背书：存在性 + 方向覆盖。airports 是背书机场（排序去重）。"""

    code: str
    kind: str  # "airport" = airports.json 有同码机场条目；"city" = 纯城市码（经机场集合背书）
    airports: tuple[str, ...]
    routes_out: bool
    routes_in: bool


def validate_codes(
    codes: list[str],
    *,
    airports: list[dict[str, Any]],
    cities: list[dict[str, Any]],
    routes: list[dict[str, Any]],
) -> tuple[dict[str, Evidence], dict[str, str]]:
    """逐码校验，返回（背书表, 拒绝原因表）。被拒的码不进背书表——缺席即不合格。

    判据（两条都过才有背书）：① 码在 airports.json（同码 airport 条目）或 cities.json；
    ② 该码（城市码经 city_code 展开、只认 iata_type=airport）在 routes.json 里有出航线
    **且**有入航线——只进不出/只出不进的机场对"城市对城市"的航班搜索都无意义。
    """
    airport_by_code = {a["code"]: a for a in airports if a.get("iata_type") == "airport"}
    airports_of_city: dict[str, set[str]] = {}
    for entry in airport_by_code.values():
        city_code = entry.get("city_code")
        if not city_code:
            continue
        airports_of_city.setdefault(city_code, set()).add(entry["code"])
    city_codes = {c["code"] for c in cities}
    out_pool = {r["departure_airport_iata"] for r in routes}
    in_pool = {r["arrival_airport_iata"] for r in routes}

    backed: dict[str, Evidence] = {}
    rejected: dict[str, str] = {}
    for code in sorted(set(codes)):
        direct = code in airport_by_code
        if not direct and code not in city_codes:
            rejected[code] = "airports/cities 两表查无此码"
            continue
        backing = ({code} if direct else set()) | airports_of_city.get(code, set())
        routes_out = bool(backing & out_pool)
        routes_in = bool(backing & in_pool)
        if not (routes_out and routes_in):
            rejected[code] = f"routes 无双向航线覆盖（出={routes_out} 入={routes_in}，背书机场 {len(backing)} 个）"
            continue
        backed[code] = Evidence(
            code=code,
            kind="airport" if direct else "city",
            airports=tuple(sorted(backing)),
            routes_out=True,
            routes_in=True,
        )
    return backed, rejected


def snapshot_document(backed: dict[str, Evidence]) -> dict[str, Any]:
    """快照文档：只留被校验到的条目（背书事实），不含全量数据集。"""
    return {
        "note": SNAPSHOT_NOTE,
        "generated_at": date.today().isoformat(),
        "sources": {"airports": AIRPORTS_URL, "cities": CITIES_URL, "routes": ROUTES_URL},
        "entries": {
            ev.code: {"kind": ev.kind, "airports": list(ev.airports)}
            for ev in sorted(backed.values(), key=lambda e: e.code)
        },
    }


def render_report(
    codes: list[str],
    backed: dict[str, Evidence],
    rejected: dict[str, str],
    *,
    airport_names: dict[str, str],
) -> str:
    """人读报告：全表模式与 --check 模式共用。背书机场最多展示 6 个，多了给计数。"""
    lines = [f"IATA 数据集校验报告（{date.today().isoformat()}）：{len(backed)} 有据 / {len(rejected)} 无据", ""]
    for code in codes:
        ev = backed.get(code)
        if ev is None:
            lines.append(f"  ✗ {code}：{rejected.get(code, '未知原因')}")
            continue
        shown = ", ".join(f"{c}({airport_names[c]})" for c in ev.airports[:6])
        extra = f" …共 {len(ev.airports)} 个" if len(ev.airports) > 6 else ""
        lines.append(f"  ✓ {code} [{ev.kind}]：{shown}{extra}")
    return "\n".join(lines)


def parse_check_pairs(items: list[str]) -> list[tuple[str, str]]:
    """--check 的 `城市=代码` 参数 → (城市, 代码) 列表；格式不对抛 ValueError。"""
    pairs: list[tuple[str, str]] = []
    for item in items:
        city, sep, code = item.partition("=")
        if not sep or not city.strip() or not code.strip():
            raise ValueError(f"--check 参数须为 城市=代码 形式，得到：{item!r}")
        pairs.append((city.strip(), code.strip().upper()))
    return pairs


def fetch_datasets(timeout: float) -> dict[str, Any]:
    """下载三张免 token 数据集。读超时放宽：静态 JSON 体积大（实测 2~14 MB）。"""
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(timeout), follow_redirects=True
    ) as client:
        payloads = {}
        for key, url in (("airports", AIRPORTS_URL), ("cities", CITIES_URL), ("routes", ROUTES_URL)):
            resp = client.get(url)
            resp.raise_for_status()
            payloads[key] = resp.json()
    return payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        nargs="*",
        metavar="城市=代码",
        help="候选行核查：只出报告，不写快照、不改表（候选补表清单只出报告的落地）",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时秒数（数据集体积大，默认放宽）")
    args = parser.parse_args(argv)

    try:
        pairs = parse_check_pairs(args.check) if args.check is not None else None
    except ValueError as exc:
        parser.error(str(exc))

    data = fetch_datasets(args.timeout)
    airport_names = {a["code"]: a.get("name", "") for a in data["airports"] if a.get("iata_type") == "airport"}

    if pairs is not None:
        backed, rejected = validate_codes([code for _, code in pairs], **data)
        print(render_report([code for _, code in pairs], backed, rejected, airport_names=airport_names))
        print("\n结论：候选行只出报告——入表是人工事实断言，自行决定是否改 city_iata.py。")
        return 0 if not rejected else 1

    table_codes = sorted({code for code in (city_iata.resolve_iata(c) for c in city_iata.known_cities()) if code})
    backed, rejected = validate_codes(table_codes, **data)
    cities_of_code: dict[str, list[str]] = {}
    for city in city_iata.known_cities():
        code = city_iata.resolve_iata(city)
        if code:
            cities_of_code.setdefault(code, []).append(city)
    print(render_report(table_codes, backed, rejected, airport_names=airport_names))
    if rejected:
        detail = "; ".join(f"{code} ← {'/'.join(cities_of_code.get(code, []))}" for code in sorted(rejected))
        print(f"\n{len(rejected)} 行无据，快照未写：{detail}")
        print("表行被数据集否决 = 事实断言不成立，人工复核 city_iata.py，不自动改表。")
        return 1
    document = snapshot_document(backed)
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    SNAPSHOT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"\n{len(backed)}/{len(table_codes)} 行全部有据 → 快照已重写 {SNAPSHOT_PATH.name}（{len(text)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
