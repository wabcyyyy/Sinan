"""交通线路的错误上游分类回放，守住旅游设施的正例。"""

from app.agent.generation.content.suggestions import build_suggestions


def test_transport_corridors_are_not_promoted_by_provider_category_or_model_suggestions():
    rejected = ["Dali–Lijiang railway", "Guangtong-Dali Railway", "Kunchuda High-Speed Railway", "广通大理铁路"]
    retained = ["中国铁道博物馆", "Railway Museum", "Ffestiniog heritage railway", "Grand Central Terminal", "金门大桥"]
    names = rejected + retained
    candidates = [
        {"name": name, "city": "大理", "category": "attraction", "kinds": "bridges,architecture,viaducts"}
        for name in names
    ]
    model_rows = [{"poi_name": name, "category": "attraction", "city": "大理"} for name in names]
    rows = build_suggestions([], candidates, [], [], model_rows, allow_external=True, dest_city="大理")
    output = {row["name"] for row in rows}
    assert output.isdisjoint(rejected)
    assert set(retained) <= output
