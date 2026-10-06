# LLM-as-judge 校准报告

- judge：`deepseek-v3` ｜ 被评：`qwen-plus`（异家族机检见 tests/test_llm_judge.py）
- judge prompt：`judge-v1.0-20260924`（sha256 9a89f8ca06ff…）
- 校准集：`calibration/human_scores.jsonl`（sha256 8726ab141e4f…，30 条）
- 生成时间：2026-09-24T02:37:21+00:00 ｜ 仅 nightly（D7：不进 PR 门禁）

| 维度 | Spearman ρ | Cohen κ | 逐值一致率 |
| --- | ---: | ---: | ---: |
| faithfulness | 0.601 | - | 23.33% |
| relevance | 0.495 | - | 66.67% |
| coherence | 0.549 | - | 30.00% |
| estimated_labels | - | 0.272 | 70.00% |
