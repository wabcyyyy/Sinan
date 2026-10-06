"""LA3 IATA 校验快照的单测：校验核心纯函数，假数据零网络（真实下载只发生在手工跑脚本时）。

验收对应（SPEC LA3）：
- 存在性判据两条都过才有背书：码在 airports/cities 两表之一 + routes 双向航线覆盖；
- 非机场条目（heliport 等 iata_type）不得给城市码背书；
- 已入库快照的形状机检：防手改出垃圾（表↔快照一致性在 test_flight_chain）。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import iata_snapshot

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "city_iata_snapshot.json"


def _fake_data() -> dict:
    """迷你数据集：覆盖直查机场 / 城市码展开 / 非机场条目 / 无航线 / 单向航线 五种形态。"""
    return {
        "airports": [
            {"code": "PEK", "iata_type": "airport", "city_code": "BJS", "name": "Beijing Capital"},
            {"code": "PKX", "iata_type": "airport", "city_code": "BJS", "name": "Beijing Daxing"},
            {"code": "HND", "iata_type": "airport", "city_code": "TYO", "name": "Haneda"},
            {"code": "NRT", "iata_type": "airport", "city_code": "TYO", "name": "Narita"},
            {"code": "LMJ", "iata_type": "heliport", "city_code": "TYO", "name": "Heliport"},
            {"code": "RRR", "iata_type": "airport", "city_code": "RRR", "name": "No routes"},
            {"code": "QQQ", "iata_type": "airport", "city_code": "QQQ", "name": "Arrival only"},
        ],
        "cities": [{"code": "BJS"}, {"code": "TYO"}, {"code": "NOWHERE"}],
        "routes": [
            {"departure_airport_iata": "PEK", "arrival_airport_iata": "HND"},
            {"departure_airport_iata": "HND", "arrival_airport_iata": "PEK"},
            {"departure_airport_iata": "NRT", "arrival_airport_iata": "PKX"},
            {"departure_airport_iata": "PKX", "arrival_airport_iata": "NRT"},
            {"departure_airport_iata": "XXX", "arrival_airport_iata": "QQQ"},
        ],
    }


def _validated(*codes: str):
    return iata_snapshot.validate_codes(list(codes), **_fake_data())


def test_direct_airport_is_backed_by_itself():
    backed, rejected = _validated("PEK")
    assert "PEK" in backed and rejected == {}
    ev = backed["PEK"]
    assert ev.kind == "airport" and ev.airports == ("PEK",)
    assert ev.routes_out and ev.routes_in, "直查机场按自身代码查双向覆盖，不搭同市兄弟机场的车"


def test_city_code_backed_by_airport_expansion():
    backed, _ = _validated("TYO", "BJS")
    tyo = backed["TYO"]
    assert tyo.kind == "city", "TYO 在 airports.json 无同码条目，是纯城市码"
    assert tyo.airports == ("HND", "NRT"), "heliport 条目 LMJ 不得背书"
    assert backed["BJS"].airports == ("PEK", "PKX"), "城市码背书 = 该市全部机场"


def test_code_in_neither_table_is_rejected():
    backed, rejected = _validated("ZZZ")
    assert backed == {}
    assert "两表查无此码" in rejected["ZZZ"]


def test_airport_without_routes_is_rejected():
    _, rejected = _validated("RRR")
    assert "无双向航线覆盖" in rejected["RRR"]


def test_one_direction_only_is_rejected():
    """只进不出的机场撑不起"城市对城市"搜索——两条方向都必须有。"""
    _, rejected = _validated("QQQ")
    assert "出=False 入=True" in rejected["QQQ"]


def test_city_without_any_airport_is_rejected():
    _, rejected = _validated("NOWHERE")
    assert "NOWHERE" in rejected


def test_snapshot_document_keeps_only_backed_entries():
    backed, _ = _validated("PEK", "TYO", "ZZZ")
    document = iata_snapshot.snapshot_document(backed)
    assert set(document) == {"note", "generated_at", "sources", "entries"}
    assert set(document["entries"]) == {"PEK", "TYO"}, "被拒的码不进快照"
    assert document["entries"]["TYO"] == {"kind": "city", "airports": ["HND", "NRT"]}
    assert document["sources"] == {
        "airports": iata_snapshot.AIRPORTS_URL,
        "cities": iata_snapshot.CITIES_URL,
        "routes": iata_snapshot.ROUTES_URL,
    }


def test_parse_check_pairs():
    assert iata_snapshot.parse_check_pairs(["杭州=HGH", " 丽江 = LJG "]) == [("杭州", "HGH"), ("丽江", "LJG")]
    for bad in ("杭州", "=HGH", "杭州="):
        try:
            iata_snapshot.parse_check_pairs([bad])
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} 应解析失败")


def test_render_report_shows_backing_and_rejections():
    backed, rejected = _validated("TYO", "ZZZ")
    report = iata_snapshot.render_report(
        ["TYO", "ZZZ"], backed, rejected, airport_names={"HND": "Haneda", "NRT": "Narita"}
    )
    assert "✓ TYO [city]：HND(Haneda), NRT(Narita)" in report
    assert "✗ ZZZ" in report


def test_committed_snapshot_shape_is_generator_output():
    """已入库快照的形状机检：手改出垃圾（坏 kind / 空机场集 / 丢元信息）在这里红。"""
    document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert set(document) == {"note", "generated_at", "sources", "entries"}
    for code, entry in document["entries"].items():
        assert len(code) == 3 and code.isupper(), f"快照键须为大写三字码：{code}"
        assert entry["kind"] in {"airport", "city"} and entry["airports"], f"{code} 条目形状坏"
