# 上线验收证据（内部）

各轮验收、P1 修复与生成实验的原始证据包（日志、JSON、截图、清单）连同结论报告。
面向用户的文档在 `docs/public/`；本目录不是产品文档，也不参与任何发布物。

## 2026-10-13 自 `docs/public/` 迁入

7 组「报告 + 证据包」用 `git mv` 成对平移，新旧位置同深度（`docs/public/` → `docs/evidence/`），
报告内的同目录相对链接与 `../../travel-agent-python/...` 代码链接均不受影响。

| 报告 | 证据包 |
| --- | --- |
| `release-audit-2026-10-11.md` | `release-audit-2026-10-11/` |
| `release-p1-fix-2026-10-11.md` | `release-p1-fix-2026-10-11/` |
| `release-content-p1-fix-2026-10-11.md` | `release-content-p1-fix-2026-10-11/` |
| `release-diet-evidence-2026-10-12.md` | `release-diet-evidence-2026-10-12/` |
| `release-category-p1-fix-2026-10-12.md` | `release-category-p1-fix-2026-10-12/` |
| `current-state-review-2026-10-12.md` | `current-state-review-2026-10-12/` |
| `generation-low-thinking-2026-10-12.md` | `generation-low-thinking-2026-10-12/` |

迁入同时删除两类不属于证据的历史产物（原因与取回方式见下）：

- `generation-low-thinking-2026-10-12/harness/`：11 个一次性实验脚本。没有任何调用方，
  且脚本按 `Path(__file__).resolve().parents[2]` 推算仓库根——放在 docs 下时该深度已错位，
  实际不可运行；它们与本地未入库的 `.tmp-release-audit/lowthink/` 逐字节相同。
- `release-audit-2026-10-11/plan.md`：描述「将要做的审计」的前瞻计划，与 AGENTS.md 哲学第 3 条
  （描述将要做的重构的文档不放仓库）冲突。

取回：`git show 1ceca1e:docs/public/<旧路径>`（删除前的最后一次提交，旧路径前缀为 `docs/public/`）。

## 关于各包的 `manifest.json`

manifest 按原样保留，不随迁移改写——审计记录只增不改，内容 sha256 与文件位置无关，仍然有效；
当前仓库内没有任何代码读取或校验这些 manifest。

- `generation-low-thinking-2026-10-12`、`release-category-p1-fix-2026-10-12`：key 是烧死的仓库根
  相对路径 `docs\public\...`。**把其中的 `docs\public\` 读作 `docs\evidence\`** 即可定位当前文件。
- `release-audit-2026-10-11`、`release-p1-fix-2026-10-11`、`release-content-p1-fix-2026-10-11`、
  `current-state-review-2026-10-12`：key 是裸文件名或包内相对名，迁移后逐字有效。
- `release-diet-evidence-2026-10-12`：本来就没有 manifest。

## 迁移时顺手修掉的链接

`release-diet-evidence-2026-10-12.md` 的 11 条证据链接原先写成裸文件名，指向的文件却在同目录证据包里，
迁入前就已失效；本次补上 `release-diet-evidence-2026-10-12/` 前缀。报告文字与数据未改动。
