# Vercel 个人展示版

展示构建复用正式 HomePage、ExplorePage、TripsPage、TripDetailPage、ChatPanel 和 MapLibre 页面。它不使用精简演示页面，也不连接 FastAPI、数据库或真实模型。

## 展示内容

- 首页对话与信息确认、固定成都两人两天一晚的逐日交付、完成预览。
- 探索页的目的地、灵感与攻略，以及个人行程列表。
- 详情页每日时间线、地图点位与导航深链、预算、收藏、标题编辑。
- 对话输入“把第二天安排松一点”，先查看差异卡，确认后移除第二天的杜甫草堂，预算 740 → 690，第一天和住宿不变。
- 演示行程和对话保存在当前标签页的 sessionStorage，刷新可续；重置体验恢复内置样例。与真实账号身份、草稿键隔离。

页面持续显示“产品交互演示 / 合成样例 / 不调用 AI”。其他城市的实时生成、其他自然语言编辑、分享、PDF、图片识别、语音转写和模型密钥配置需要真实后端；展示版不执行这些操作，不把固定结果冒充模型输出。地图底图仍依赖外网，失败时显示地图错误提示，行程和导航深链可继续使用。

## 本地预览

在 `travel-frontend-react` 运行：

```bash
npm ci
npm run build:demo
npm run preview
```

打开终端给出的地址，首页就是完整展示版。常规 `npm run dev` / `npm run build` 保留真实后端模式；其中 `/demo` 及 `/demo/trips/101` 是相同完整页面的隔离演示入口。

## 发布到 Vercel

当前个人展示站：[sinan-demo.vercel.app](https://sinan-demo.vercel.app)，项目 `wabcy/sinan-demo`，使用 Hobby 套餐。2026-10-16 展示适配迁移到远端 master `eb151681d71c3addacf620b1283faa4f07078a04`，保留该版本的首页照片拼贴、Motion 动效和详情页重排。Git 自动部署已连接 `wabcyyyy/Sinan`，生产分支为 `master`；完整展示适配已从 `codex/vercel-latest-frontend` 合并到该分支。

### 自动发布更新

将通过验证的修改合并并推送到 GitHub 的 `master` 分支，Vercel 会自动构建，成功后更新 `sinan-demo.vercel.app`。其他分支的推送用于预览；要更新公开展示站，需合并到 `master`。没有推送的本地修改不会上线。

本次整合工作区为 `C:/Users/25924/.codex/worktrees/vercel-latest-frontend/Sinan`，后续可从独立分支提交并通过 Pull Request 合并到 `master`。原 `D:/wcy/project/Sinan` 的未提交工作保留，本地 `master` 尚未更新；不要在旧工作区强行拉取或覆盖。

Vercel 项目设置：

| 设置 | 值 |
| --- | --- |
| Git repository | `wabcyyyy/Sinan` |
| Production branch | `master` |
| Root Directory | `travel-frontend-react` |
| Framework | Vite |
| Build Command | `npm run build:demo` |
| Output Directory | `dist` |

`build:demo` 读取入仓的 `.env.demo`，其中仅含公开开关 `VITE_DEMO_MODE=true`。不需要数据库密码、JWT 或模型 Key，也无需购买域名。`.vercel/` 与 `.env.local` 是本机配置，不入仓。

在 [Vercel 项目部署列表](https://vercel.com/wabcy/sinan-demo/deployments) 查看对应 Git 提交的状态。失败时检查日志，站点继续使用上一版成功部署；需要重试时在部署详情中使用 Redeploy。Git 集成必须保留对 `Sinan` 仓库的授权。

发布成功后仍须验证公开 URL 和目标网络体验；构建通过本身不能说明浏览器交互或外部地图正常。

Vercel 官方说明：[Hobby 计划](https://vercel.com/docs/plans/hobby)、[Vite 与 SPA 路由](https://vercel.com/docs/frameworks/frontend/vite)。
