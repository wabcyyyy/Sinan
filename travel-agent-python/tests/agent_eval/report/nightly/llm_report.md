# 真实 LLM 评测报告

- 生成路径：`stream` ｜ model：`qwen-plus` ｜ temperature：0.4 ｜ open_day prompt：`v1.1.narrative` ｜ open_trip prompt：`v1.1.narrative`
- 用例数：3 ｜ 两遍一致率：0.00%

## 北京-1d（北京 1 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 100.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 41.18% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 5 |

## 上海-2d（上海 2 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 90.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 83.43% |
| coord_valid_rate | 92.31% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 10 |

## 杭州-3d（杭州 3 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 52.17% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 5.56% |
| route_violation_rate | 100.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 88.72% |
| coord_valid_rate | 26.09% |
| deeplink_resolvable_rate | 66.67% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 40.00% |
| pending_review_count | 22 |
