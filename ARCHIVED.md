# Java 版后端已删除（Archived & Removed）

`travel-backend-java/` 是本项目的**第一版后端实现**：Spring Boot 4.1 + MyBatis-Plus，承载认证 / 行程状态机 / 缓存 / 异步 PDF / SSE 网关，共 50 个业务端点。

后端已按绞杀者路线整体迁到 **FastAPI 单进程**（`travel-agent-python`：业务面 `/api/**` + Agent 面 `/api/agent/v1/**` 同进程直调）。迁移完成后，该模块**已从工作树删除**（2026-09-14），不再有第二份实现需要同步。

## 现在谁负责什么

| 原先在 Spring 侧 | 现在的位置 |
| --- | --- |
| 认证 / 会话签发与吊销 | `travel-agent-python/app/api/business/auth.py` + `app/api/deps.py` + `app/services/user_service.py` |
| 行程状态机 / 读写 / 版本快照 | `app/services/itinerary_{query,command,version,plan_apply}.py` |
| 生成编排（逐日落库、缺天修复、自动续跑） | `app/services/{itinerary_generation,day_persistence,generation_recovery}.py` |
| 对话改行程 + 草稿版本 | `app/services/itinerary_chat.py` |
| 详情缓存 / 限速 / 日锁 / 令牌黑名单 | `app/services/{state_and_sessions,generation_gate}.py` + `app/common/{cache_store,redis_client,token_revocation}.py`（共享同一套 Redis 键） |
| SSE 网关（Redis 订阅转发） | `app/common/event_hub.py`（进程内事件总线，不再需要 Redis 中转） |
| 异步 PDF 导出（Thymeleaf 模板 + Flying Saucer） | `app/services/export_{service,pdf}.py`（reportlab 排同一套版式；字体在 `app/resources/fonts/`） |
| schema 迁移执行器（Flyway） | `app/db/migrate.py`（Alembic 按序执行同一批 SQL，现在位于 `app/db/migrations/sql/`，服务启动即迁移） |
| 管理后台 / 探针端点 | `app/api/business/{admin,probe}.py` + `app/services/admin_service.py` |

## 想找回旧代码

全部内容都在 git 历史里（删除是一次普通提交，未做历史重写）：

```bash
git log --oneline -- travel-backend-java | head        # 找到删除前的最后一个提交
git show <commit>:travel-backend-java/pom.xml          # 按需查看单个文件
git checkout <commit> -- travel-backend-java           # 或整体取回工作树
```

注意：旧模块里的 `db/migration/V*.sql` 与 `fonts/simhei.ttf` 在迁移时已 `git mv` 到 `travel-agent-python`（全仓只留一份，避免双份真相漂移），因此从更早的提交整体取回时会自动带上它们；同时**不要**让两端 SQL 长期并存——Flyway 的 `validate-on-migrate: true` 会校验已发布文件的 checksum，两份副本迟早分叉。

另外两处不入库的遗留物已在删除前另存：

- `docs/archive-java-backend.env`：旧模块的 `.env`（含当时的 JWT/MYSQL 配置），仅本机保留，恢复旧服务时可参考；
- 旧模块 `data/export/` 下 22 个已导出的行程 PDF，已移入 `travel-agent-python/data/export/`。

## 迁移记录

- 端点对照的历史结论：50 个业务端点，49 个等价落地，1 个是刻意退役的内部探针 `/api/test/call-agent`。`travel-agent-python/scripts/check_endpoint_coverage.py` 在 Java 侧不存在时报告"端点对照不再适用且无残留登记"（`EXPECTED_REMAINING` 已清空；该脚本已于 2026-09-18 删除，理由见 `tests/test_retirement_readiness.py` 模块 docstring）
- 迁移后的架构说明：根 `README.md`、`travel-agent-python/README.md`、`travel-frontend-react/README.md`（`docs/项目介绍/` 为本地内部文档，不入库）
