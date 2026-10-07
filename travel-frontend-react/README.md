# travel-frontend-react

React 19 + Vite + TypeScript 壳（入口 `src/react-app/`，`main.tsx` 挂载 `App.tsx`）：首页 ChatIntake 一句话对话创建（澄清卡 + 槽位确认条）、生成期实时预览、行程列表、详情页 ChatPanel 对话编排 + 地图。`src/` 下 Vue3 组件树已整体删除（2026-09-27），只剩 React 消费集：`api/`（后端只经 `api/sinan.ts` 访问）、`shared/`（框架无关纯函数与地图深链）、`types/`（手镜像 + 生成的契约）、`styles/`（令牌与首帧外观）、`assets/`。

## 技术栈

- React 19.2 + Vite 7 + TypeScript；**无路由库 / 无全局状态库**：路由由 `src/react-app/router.ts`（History API）+ `App.tsx` 按路径分发，状态在组件 hooks
- MapLibre GL：**OpenFreeMap 免 key 在线矢量底图**（亮/暗随外观切换，署名随图）；点位钉与坐标有效性谓词在 `react-app/itinerary/mapPins.ts`，地图深链白名单在 `shared/map-link.ts`
- 不引组件库：Element Plus 已零用量（`npm run ep:lint` 钉死防回潮），交互控件页面内自绘
- 自托管字体（`public/fonts/`：Geist Sans 拉丁子集 + Poppins 回退，中文走系统栈；首屏零第三方字体请求）
- 测试：vitest + happy-dom（纯逻辑 + `renderToStaticMarkup` 静态渲染断言）；金路径 E2E 走 playwright-core（`npm run e2e:golden`）

## 页面与路由

| 路由 | 页面 | 说明 |
| --- | --- | --- |
| `/` | HomePage | 首页：ChatIntake 一句话收集槽位（缺槽自动追问、确认卡拍板）+ 规划草稿实时预览 |
| `/login` | LoginPage | 登录 / 注册 |
| `/explore`、`/explore/guide/:slug` | ExplorePage / GuideDetailPage | 探索页三合一（灵感 / 目的地 / 攻略）；旧 `/destinations` `/inspiration` `/guides` 自动 replace 重定向并入 |
| `/trips` | TripsPage | 行程列表（状态 Tab 筛选 / 搜索 / 收藏 / 草稿区） |
| `/trips/:id` | TripDetailPage | 详情：ChatPanel 对话编排（草稿卡确认后应用）+ TripMapPanel 地图与深链核实外链 |
| `/settings` | SettingsPage | LLM 网关管理（用户新增 / 启停 / 连通性测试） |
| `/s/:token` | SharePage | 公开只读分享页 |

旧创建页 `/plan`、`/generate` 已退役：直达自动回首页（保留书签查询参数，见 `router.ts`）。

## 启动

```bash
cp .env.example .env   # 空白模板：前端不需要任何第三方 key
npm install
npm run dev            # http://localhost:5173，/api 代理到 8000（FastAPI）
```

构建：`npm run build`（vite build）。

## 门禁

```bash
npm run test:unit    # vitest（纯逻辑 + 组件静态渲染；happy-dom）
npm run theme:lint   # 外观契约：禁裸色/野 z-index/!important；豁免表只准变短（当前清零）
npm run ep:lint      # 去 EP：产品面禁引入 Element Plus，允许清单只减不增（当前清零）
npm run e2e:golden   # 金路径 E2E（先 start-all.ps1 起活栈；真实 LLM 生成，分钟级）
```

## 环境变量

| 变量 | 说明 |
| --- | --- |
| （无） | 前端不需要任何 key：地图为 OpenFreeMap 免 key 在线矢量瓦片（署名随图），点位/封面图经服务端同源代理获取（图库 key 只在服务端） |
