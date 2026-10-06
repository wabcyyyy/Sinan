"""深链语义 parity（Y4）：用 tests/golden/deeplink_cases.json 钉住后端地图深链口径。

前端断言在同仓 travel-frontend-react/src/react-app/itinerary/deeplink-parity.test.ts，
读**同一份 case 文件**。2026-10-04 React 迁移后两侧职责不对称：搜索/路线深链只有
本模块实现；前端只按 pin 出高德 marker / 谷歌查询两条固定「核实」链接（经
shared/map-link 白名单），无有效性谓词、无路线深链——机制性差异（R1-R4）逐条见
tests/golden/deeplink_parity.md 差异表，下表 DIFF_NOTES 是其在断言层的落点。
任何一侧单方面改语义都会让对侧或本侧显形。断言粒度 = 协议 host + path 前缀 +
关键查询参数存在/缺席——src/callnative/policy/coordinate 与坐标数值（GCJ-02
换算等）属实现细节，不做整串相等。
"""

from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qs, urlsplit

import pytest

from app.agent.data import map_link
from app.agent.data.map_link import map_directions_url, map_search_url, to_gcj02

CASES_PATH = pathlib.Path(__file__).parent / "golden" / "deeplink_cases.json"

#: 与 deeplink_parity.md 差异表一一对应：两侧期望不同的 case 必须在此登记，
#: 值只写机制类编号 + 一句结论，细节以差异表为准。R1/R3/R4 = 2026-10-04 React
#: 迁移后的机制性差异（前端深链面收窄为 pin 级固定双出口）；原 R2（前端无
#: 有效性谓词）已于同日拍板补齐——前端 hasValidCoords 与后端 _parse_coords
#: 同语义，哨兵/越界 case 两侧期望同为"无出口"，不再构成差异。
_DIFF = {
    "search_domestic_with_coords": "R1 前端恒出双出口；高德 marker 两侧同形态（GCJ-02 仅后端换算，断言粒度外）",
    "search_foreign_with_coords": "R1 前端恒出双出口（额外提供高德 marker）",
    "search_zero_sentinel": "R3 无效坐标视同无坐标：前端不进 pin、后端回落关键词",
    "search_out_of_range_coords": "R3 同上",
    "search_domestic_no_coords": "R3 无坐标不进 pin、无出口；后端关键词搜索",
    "search_foreign_no_coords": "R3 同上",
    "search_hanzi_foreign_city_no_coords": "R3 同上",
    "route_domestic_two_stops": "R4 前端路线深链已退役（核实退化为逐点）；后端保留",
    "route_foreign_two_stops": "R4 同上",
    "route_domestic_three_stops": "R4 同上",
    "route_foreign_five_stops": "R4 同上（原 D6 waypoints 上限分歧随之消解）",
    "route_foreign_seven_stops": "R4 同上",
    "route_mixed_validity_stops": "R4 同上",
    "route_out_of_range_stop": "R4 同上",
}
DIFF_NOTES = _DIFF

#: D3 判定字典的固定钉：parity 测试不依赖真实 city_geo 库内容（离线套件的库状态
#: 不确定），把字典查询钉成确定性映射后测链接逻辑本身；DB 查询路径由
#: test_place_dict_lookup 单独覆盖。
_CITY_DICT = {
    "上海": True,
    "杭州": True,
    "北京": True,
    "巴厘岛": False,
    "Paris": False,
    "Tokyo": False,
    "New York": False,
}

#: 导入期保存真实实现：DB 路径测试要先撤掉 _pin_city_dict 的钉再测原函数。
_real_dict_domestic = map_link._dict_domestic


@pytest.fixture(autouse=True)
def _pin_city_dict(monkeypatch):
    monkeypatch.setattr(map_link, "_dict_domestic", lambda city: _CITY_DICT.get(str(city or "").strip()))


def test_place_dict_lookup_reads_city_geo(monkeypatch):
    """_dict_domestic 的字典路径：命中→布尔、未收录→None、库不可用→None。

    读法已从 map_link 自带的 `session_scope` 裸查收敛到 `city_reference.get_city_geo`
    （城市字典唯一入口，含"失败吞成 None、不中断生成链路"的既有契约）。
    """
    monkeypatch.setattr(map_link, "_dict_domestic", _real_dict_domestic)

    rows = {
        "杭州": {"city_name": "杭州", "is_domestic": 1},
        "巴厘岛": {"city_name": "巴厘岛", "is_domestic": 0},
        # "不存在城" 故意不在表里：字典未收录必须与"库不可用"同样回 None
    }
    monkeypatch.setattr(map_link.city_reference, "get_city_geo", lambda city: rows.get(city))
    assert map_link._dict_domestic("杭州") is True
    assert map_link._dict_domestic("巴厘岛") is False
    assert map_link._dict_domestic("不存在城") is None
    assert map_link._dict_domestic("") is None, "空城市不打字典"


def test_to_gcj02_matches_frontend_known_values():
    """跨语言一致性测试向量：钉住 GCJ-02 换算式的已知值防回退。

    （前端 React 端已无换算实现——深链直接用后端下发坐标，见差异表 R1；
    原"与前端 coordinates.toGcj02 同断言"的对照关系随 Vue 侧退役终结。）
    """
    lat, lon = to_gcj02(39.9, 116.4)
    assert round(lat, 6) == 39.901404
    assert round(lon, 6) == 116.406243
    assert to_gcj02(35, 139) == (35, 139)  # 境外原样返回


def _cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def _assert_protocol(url: str | None, expect: dict, note: str = "") -> None:
    suffix = f"（{note}）" if note else ""
    if expect.get("null"):
        assert url is None, f"应返回空{suffix}"
        return
    assert url is not None, f"应产出链接{suffix}"
    parts = urlsplit(url)
    assert parts.hostname == expect["host"], f"{url}{suffix}"
    assert parts.path.startswith(expect["path"]), f"{url}{suffix}"
    query = parse_qs(parts.query)
    for key in expect.get("params", []):
        assert query.get(key), f"missing {key!r}: {url}{suffix}"
    for key in expect.get("absent_params", []):
        assert key not in query, f"unexpected {key!r}: {url}{suffix}"


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_deeplink_backend_protocol(case):
    """后端 map_link 深链按 golden case 钉协议口径；两侧差异点见 DIFF_NOTES 与差异表。"""
    expect = case["backend"]
    stops = case["input"].get("stops")
    if stops is not None:
        url = map_directions_url([dict(stop) for stop in stops])
    else:
        url = map_search_url(
            case["input"]["name"],
            case["input"]["city"],
            latitude=case["input"].get("latitude"),
            longitude=case["input"].get("longitude"),
        )
    _assert_protocol(url, expect, DIFF_NOTES.get(case["name"], ""))
