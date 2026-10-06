# 主题化评测报告（真实 LLM）

- model：`qwen-plus` ｜ temperature：0.4 ｜ open_day prompt：`v1.1.narrative` ｜ open_trip prompt：`v1.1.narrative`
- 用例数：6 ｜ 两遍一致率：16.67%

## kyoto-baseline（京都 3 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | 100.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 0.00% |
| pending_review_count | 11 |

## kyoto-themed（京都 3 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 0.00% |
| field_reference_rate | 100.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 0.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | 66.67% |
| poi_relevance | 100.00% |
| coord_available_rate | 0.00% |
| pending_review_count | 12 |

## hangzhou-baseline（杭州 1 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 28.57% |
| field_reference_rate | 50.00% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 100.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## hangzhou-themed（杭州 1 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：degraded ｜ 两遍一致：是

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 33.33% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 100.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | 0.00% |
| poi_relevance | 0.00% |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## chengdu-baseline（成都 2 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 100.00% |
| field_reference_rate | 28.57% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 100.00% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | - |
| poi_relevance | - |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |

## chengdu-themed（成都 2 日）

- prompt_version：`v1.1.narrative/v1.1.narrative` ｜ run1 status：degraded ｜ run2 status：degraded ｜ 两遍一致：否

| 指标 | 结果 |
| --- | ---: |
| poi_authority_rate | 80.00% |
| field_reference_rate | 14.29% |
| time_conflict_rate | 0.00% |
| route_violation_rate | 85.71% |
| attraction_duplicate_rate | 0.00% |
| budget_deviation_rate | 0.00% |
| theme_sentence_rate | 100.00% |
| why_coverage | 100.00% |
| practical_notes_rate | 100.00% |
| theme_hit_rate | 33.33% |
| poi_relevance | 0.00% |
| coord_available_rate | 100.00% |
| pending_review_count | 0 |
