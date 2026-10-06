"""SPEC C3.2 eval 扩容：深度指标口径与 mock 坐标参数化（纯函数，无网络）。

断言焦点：
1. coord_valid_rate：attraction/food/hotel 项的坐标有效率（0/0 哨兵与 None 均无效，
   transport 项不参与）；
2. deeplink_resolvable_rate：≥2 个有效坐标停靠点的天必须能生成全天路线深链；
   不足 2 点的天按"无路线承诺"自动通过（无坐标行程不因此红）；
3. category_reasonable_rate：item_type 白名单 + poi_name 非空；
4. mock_llm 坐标参数化：汉字城市落国内国界框内、非汉字城市落海外、
   coords=False 变体无坐标——国内外/无坐标语义因此可离线评测。
"""

from __future__ import annotations

from app.schemas.trip import DailyPlan, GenerateResponse, TripItem
from tests.agent_eval import mock_llm
from tests.agent_eval.metrics import evaluate_depth


def _item(name: str, item_type: str = "attraction", lat: float | None = 30.1, lon: float | None = 120.2) -> TripItem:
    return TripItem(
        item_type=item_type,
        poi_name=name,
        start_time="09:00",
        end_time="10:00",
        latitude=lat,
        longitude=lon,
    )


def _response(plans: list[DailyPlan]) -> GenerateResponse:
    return GenerateResponse(city="杭州", days=len(plans), title="测试行程", daily_plans=plans, budget_estimate={})


def test_depth_all_valid_coords_and_routes():
    plan = DailyPlan(
        day_no=1,
        items=[
            _item("西湖"),
            _item("雷峰塔", lat=30.15, lon=120.15),
            _item("楼外楼", item_type="food", lat=30.2, lon=120.1),
        ],
    )
    depth = evaluate_depth(_response([plan]), {"city": "杭州"})
    assert depth["coord_valid_rate"] == 1.0
    assert depth["deeplink_resolvable_rate"] == 1.0
    assert depth["category_reasonable_rate"] == 1.0


def test_depth_no_coords_keeps_deeplink_green_and_coord_red():
    plan = DailyPlan(
        day_no=1,
        items=[_item("西湖", lat=None, lon=None), _item("楼外楼", item_type="food", lat=None, lon=None)],
    )
    depth = evaluate_depth(_response([plan]), {"city": "杭州"})
    assert depth["coord_valid_rate"] == 0.0
    # 无坐标天没有路线承诺：不足 2 个有效停靠点自动通过，不冤枉无坐标变体
    assert depth["deeplink_resolvable_rate"] == 1.0
    assert depth["category_reasonable_rate"] == 1.0


def test_depth_dirty_coords_fail_has_coord_and_day_auto_passes():
    """脏坐标（不可 float）按 has_coord 失败计：坐标率红；天不足 2 个有效停靠点
    → 无路线承诺自动通过。map_directions_url 自身失败的分支由未来改动触发
    （本指标同时是深链实现的回归绊线）。"""

    class _DirtyItem:
        """带 model_dump 的 duck-type：坐标是不可 float 的脏值。"""

        item_type = "attraction"

        def __init__(self, name: str, lat, lon):
            self.poi_name = name
            self.latitude = lat
            self.longitude = lon
            self.start_time = "09:00"
            self.end_time = "10:00"

        def model_dump(self) -> dict:
            return {
                "item_type": self.item_type,
                "poi_name": self.poi_name,
                "latitude": self.latitude,
                "longitude": self.longitude,
            }

    plan = DailyPlan.model_construct(
        day_no=1,
        items=[_DirtyItem("甲", "abc", "120.1"), _DirtyItem("乙", "30.2", "xyz")],
    )
    response = GenerateResponse.model_construct(city="杭州", days=1, daily_plans=[plan])
    depth = evaluate_depth(response, {"city": "杭州"})
    assert depth["coord_valid_rate"] == 0.0
    assert depth["deeplink_resolvable_rate"] == 1.0
    assert depth["category_reasonable_rate"] == 1.0


def test_depth_transport_items_do_not_participate_in_coord_rate():
    plan = DailyPlan(
        day_no=1,
        items=[
            _item("西湖"),
            _item("雷峰塔", lat=30.15, lon=120.15),
            _item("地铁1号线", item_type="transport", lat=None, lon=None),
        ],
    )
    depth = evaluate_depth(_response([plan]), {"city": "杭州"})
    # transport 不计入坐标有效率的分母，但计入类目合理率（白名单值）
    assert depth["coord_valid_rate"] == 1.0
    assert depth["category_reasonable_rate"] == 1.0


def test_mock_coords_domestic_overseas_and_none_variant():
    domestic = mock_llm.catalog("杭州")["attractions"][0]
    assert 18 <= domestic["latitude"] <= 54 and 73 <= domestic["longitude"] <= 135
    overseas = mock_llm.catalog("Barcelona")["attractions"][0]
    assert not (18 <= overseas["latitude"] <= 54 and 73 <= overseas["longitude"] <= 135)
    for row in mock_llm.catalog("杭州", coords=False)["attractions"]:
        assert row["latitude"] is None and row["longitude"] is None
    # 海外无坐标变体同样成立（eval 用例的 coords 字段透传到 fixture）
    for row in mock_llm.catalog("Paris", coords=False)["foods"]:
        assert row["latitude"] is None and row["longitude"] is None
