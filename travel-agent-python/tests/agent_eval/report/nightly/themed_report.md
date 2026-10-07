# 主题化评测报告（真实 LLM）

- 生成路径：`stream` ｜ model：`deepseek-flash` ｜ temperature：0.4 ｜ open_day prompt：`v1.4.localized` ｜ open_trip prompt：`v1.3.localized`
- 用例数：6 ｜ 两遍一致率：66.67%
- 两个口径别混读：`route_violation_rate` 量的是**业务终检前**的中间产物（评测只调 run_generate_trip_stream，不跑落库后的 validate_plans）；`terminal_reset_day_rate` 用生产同一判官算「生产会重置几成天」——那才是用户实际会撞到的缺口信号（重置天要重烧一次 LLM，或最终落 PENDING）。

## kyoto-baseline（京都 3 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.29% |
| terminal_reset_day_rate | 66.67% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 33.33% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## kyoto-themed（京都 3 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：failed ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 27.78% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 22.22% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | 50.00% |
| poi_relevance | 60.00% |
| coord_available_rate | 0.00% |
| pending_review_count | 14 |

## hangzhou-baseline（杭州 1 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：failed ｜ run2 status：failed ｜ 两遍一致：是

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | - |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 79.07% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 0.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 0.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 0.00% |
| practical_notes_rate | 0.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 0.00% |
| pending_review_count | 0 |

## hangzhou-themed（杭州 1 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：failed ｜ run2 status：failed ｜ 两遍一致：是

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | - |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 79.07% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 0.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 0.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 0.00% |
| practical_notes_rate | 0.00% |
| theme_hit_rate | 0.00% |
| poi_relevance | 0.00% |
| coord_available_rate | 0.00% |
| pending_review_count | 0 |

## chengdu-baseline（成都 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：failed ｜ run2 status：failed ｜ 两遍一致：是

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | - |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 75.86% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 0.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 0.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 0.00% |
| practical_notes_rate | 0.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 0.00% |
| pending_review_count | 0 |

## chengdu-themed（成都 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：failed ｜ run2 status：failed ｜ 两遍一致：是

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | - |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 75.86% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 0.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 0.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 0.00% |
| practical_notes_rate | 0.00% |
| theme_hit_rate | 0.00% |
| poi_relevance | 0.00% |
| coord_available_rate | 0.00% |
| pending_review_count | 0 |
