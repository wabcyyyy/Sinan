# 开发与验证

需要 Python 3.12、uv、Node 22.12+。CI 与 Docker 使用 Node 22，依赖以锁文件为准。

先按 README 初始化配置，执行 `docker compose up -d mysql redis`。
后端目录运行 `uv sync --frozen`，再运行 `uv run python main.py`。
前端目录运行 `npm ci`，再运行 `npm run dev`。单看固定回放只需前端，打开 `/demo`。
宿主机连接基础设施时检查 DB_PORT/REDIS_PORT 与根环境文件一致。

## 后端

在 `travel-agent-python/` 执行完整门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

包含 Ruff、类型、分层、规模、秘密扫描、离线测试、变更行覆盖率与契约漂移，CI 在 Linux 执行对应步骤。
行为变更另执行：

```bash
uv run python tests/agent_eval/eval_agent.py
uv run python tests/agent_eval/eval_research.py
uv run python tests/agent_eval/eval_editing.py
uv run python tests/agent_eval/eval_experience.py
uv run python scripts/eval_ratchet.py
```

`eval_agent.py` 默认走业务层产品路径（`plan_days` 在临时 SQLite 上真跑，按落库读回的交付结果计分）；`--path graph` 跑 Agent 面的 trip 分支作对照，报告另存 `report_graph.*`，不进棘轮。评测期间 LLM HTTP 出口有哨兵，任何真实调用都会让该用例报错。

schema 变更执行 `uv run python scripts/export_contracts.py`，生成产物随同批提交。迁移只追加，门禁不放宽。
真实 API 测试、nightly 和生成压测可能调用模型，不属于默认离线门禁。

## 前端

在 `travel-frontend-react/` 执行：

```bash
npm run build
npm run test:unit
npm run theme:lint
npm run ep:lint
npm run e2e:demo
```

浏览器测试需要前端运行与 Edge/Chrome，默认 Edge，可设 E2E_CHANNEL=chrome。
CI 安装 Playwright Chromium 并设 E2E_CHANNEL=bundled。回放测试阻断业务 API 与外部资源，不调用模型。
`e2e:golden` 是真实生成路径，不要与固定回放混淆。

## 字体维护

从 provenance.json 指定的官方提交下载原始字体，在后端运行 `uv run python scripts/build_preview_font.py SOURCE.ttf`。
脚本验证原始 SHA256，输出静态 TTF 与台账。fontTools 仅为开发依赖；运行镜像使用已提交字体与 OFL。

Docker 构建通过 Compose 提供只含根 LICENSE/第三方说明的 named context，保证镜像保留许可。
单独构建 Dockerfile 时也要传入 `--build-context project=..`（在对应应用目录执行）。
