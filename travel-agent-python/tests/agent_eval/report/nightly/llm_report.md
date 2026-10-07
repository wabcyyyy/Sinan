# 真实 LLM 评测报告

- 生成路径：`stream` ｜ model：`deepseek-flash` ｜ temperature：0.4 ｜ open_day prompt：`v1.4.localized` ｜ open_trip prompt：`v1.3.localized`
- 用例数：13 ｜ 两遍一致率：0.00%
- 两个口径别混读：`route_violation_rate` 量的是**业务终检前**的中间产物（评测只调 run_generate_trip_stream，不跑落库后的 validate_plans）；`terminal_reset_day_rate` 用生产同一判官算「生产会重置几成天」——那才是用户实际会撞到的缺口信号（重置天要重烧一次 LLM，或最终落 PENDING）。

## 北京-1d（北京 1 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | - |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 0.00% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 0.00% |
| pending_review_count | 6 |

## 上海-2d（上海 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 50.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 50.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## 杭州-3d（杭州 3 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 41.67% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 50.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 66.67% |
| coord_valid_rate | 41.67% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 42.86% |
| pending_review_count | 7 |

## 成都-4d（成都 4 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 5.88% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 25.00% |
| coord_valid_rate | 5.88% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 0.00% |
| pending_review_count | 16 |

## 西安-2d（西安 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 0.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## 三亚-5d（三亚 5 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 12.50% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 12.50% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 0.00% |
| pending_review_count | 21 |

## 广州-2d（广州 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 5.88% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 5.88% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 9.09% |
| pending_review_count | 17 |

## 厦门-3d（厦门 3 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 57.14% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 33.33% |
| coord_valid_rate | 57.14% |
| deeplink_resolvable_rate | 66.67% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 42.86% |
| pending_review_count | 6 |

## Barcelona-3d（Barcelona 3 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 63.64% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 15.14% |
| terminal_reset_day_rate | 100.00% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 66.67% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## Tokyo-2d（Tokyo 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 15.38% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 50.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 50.00% |
| coord_valid_rate | 30.77% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 37.50% |
| pending_review_count | 11 |

## Bali-4d（Bali 4 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 10.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 11.94% |
| terminal_reset_day_rate | 75.00% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## 昆明-2d（昆明 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 28.57% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 50.00% |
| coord_valid_rate | 100.00% |
| deeplink_resolvable_rate | 50.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## Paris-2d（Paris 2 日）

- prompt_version：`v1.4.localized/v1.3.localized` ｜ run1 status：success ｜ run2 status：success ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 70.00% |
| field_reference_rate | 0.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| terminal_reset_day_rate | 0.00% |
| coord_valid_rate | 90.00% |
| deeplink_resolvable_rate | 100.00% |
| category_reasonable_rate | 100.00% |
| theme_sentence_rate | 0.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 83.33% |
| pending_review_count | 3 |
