# AGENTS.md — travel-frontend-react

入口是 React 19 壳（`src/react-app/`）：首页 ChatIntake 对话式收集槽位并实时预览、详情页 ChatPanel 对话编排（确认卡长在对话流内）。`src/` 下 Vue3 组件树已整体删除（2026-09-27），只剩 React 消费集：`api/sinan.ts`、`shared/`（纯函数证据与地图深链）、`types/`（手镜像 + 生成的契约）、`styles/`（令牌与首帧外观）、`assets/`。MapLibre + OpenFreeMap 免 key 底图。先读根 `../AGENTS.md`。

## 外观门禁（只减不增，CI 已接）

- `npm run theme:lint`：禁裸色值与野 z-index；豁免表（`scripts/theme-lint.allowlist.json`）已清零，保持为空。新颜色必须走令牌变量。
- `npm run ep:lint`：Element Plus 用量清单（`scripts/ep-allowlist.json`）已清零，产品面禁止引入 EP。

## 结构惯例

- **后端只经 `src/api/sinan.ts` 访问**：URL、信封解包、类型集中在这一处；组件里禁止散落裸 `fetch`/`EventSource`（`test_cutover_contract` 会扫 `src/api/*.ts`）。
- **组件拆分**：页面组件只做编排，可复用区块拆到 `react-app/<域>/` 并配 `.test.ts`（renderToStaticMarkup 静态渲染断言，零新测试依赖）。
- **契约类型只引 `src/types/generated/contracts.ts`**；视图层手镜像类型放 `src/types/`，不手写契约里已有的形状。
- **外观写入口唯一**：主题/密度/动效切换只经 `src/styles/appearance.ts`（index.html 的首帧引导脚本是它的无依赖镜像），别绕过它直接操作 DOM 样式。

## 命令

```bash
npm run dev        # 本地开发（/api 代理到 :8000）
npm run typecheck  # tsc --noEmit（含 src/**/*.tsx，strict 全开）
npm run build      # typecheck + vite 构建（CI 的前端 job 跑的就是它）
npm run test:unit  # vitest
npm run theme:lint # 外观门禁
npm run ep:lint    # EP 用量门禁
npm run e2e:golden # 金路径 E2E（先 start-all.ps1 起活栈；真实 LLM 生成，分钟级）
```
