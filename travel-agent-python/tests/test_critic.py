from app.agent.generation.output.critic import critique_plans


def test_critic_reports_soft_quality_without_replacing_hard_validator():
    result = critique_plans(
        [
            {
                "day_no": 1,
                "items": [
                    {"item_type": "attraction", "poi_name": "西湖", "tag": "自然风光"},
                    {"item_type": "food", "poi_name": "本地餐厅"},
                ],
            }
        ],
        ["自然风光"],
    )
    assert result.score > 0.7
    assert result.dimensions["preference_alignment"] == 1.0
    assert result.strengths


def test_critic_explains_empty_or_overloaded_days():
    result = critique_plans(
        [
            {"day_no": 1, "items": []},
            {
                "day_no": 2,
                "items": [
                    {"item_type": "attraction", "poi_name": "A"},
                    {"item_type": "attraction", "poi_name": "B"},
                    {"item_type": "attraction", "poi_name": "C"},
                    {"item_type": "attraction", "poi_name": "D"},
                ],
            },
        ]
    )
    assert any("缺少" in issue or "密度" in issue for issue in result.issues)
