# 真实 LLM 评测报告

- 生成路径：`stream` ｜ model：`deepseek-flash` ｜ temperature：0.4 ｜ open_day prompt：`v1.5.localized` ｜ open_trip prompt：`v1.4.localized`
- 用例数：3 ｜ 两遍一致率：0.00%
- 两个口径别混读：`route_violation_rate` 量的是**业务终检前**的中间产物（评测只调 run_generate_trip_stream，不跑落库后的 validate_plans）；`terminal_reset_day_rate` 用生产同一判官算「生产会重置几成天」——那才是用户实际会撞到的缺口信号（重置天要重烧一次 LLM，或最终落 PENDING）。

## 北京-1d（北京 1 日）

- prompt_version：`v1.5.localized/v1.4.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 57.14% |
| field_reference_rate | - |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 57.14% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 80.00% |
| pending_review_count | 3 |

## 上海-2d（上海 2 日）

- prompt_version：`v1.5.localized/v1.4.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 91.67% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 88.89% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 1 |

## 杭州-3d（杭州 3 日）

- prompt_version：`v1.5.localized/v1.4.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 60.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 65.00% |
| deeplink_resolvable_rate | 33.33% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 83.33% |
| pending_review_count | 8 |
