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

当前个人展示站：[sinan-demo.vercel.app](https://sinan-demo.vercel.app)，项目 `wabcy/sinan-demo`，使用 Hobby 套餐。2026-10-16 展示适配迁移到远端 master `eb151681d71c3addacf620b1283faa4f07078a04`，保留该版本的首页照片拼贴、Motion 动效和详情页重排。发布目录为本次独立 worktree 中的 `travel-frontend-react`，尚未连接 Git 自动部署。

### 从本机更新

在已登录 Vercel CLI 的本机，从 `travel-frontend-react` 执行：

```bash
vercel deploy --prod --scope wabcy --build-env VITE_DEMO_MODE=true
```

本机目录已链接项目；其他机器先执行 `vercel login`，再执行 `vercel link --yes --scope wabcy --project sinan-demo`。`.vercel/` 和 `.env.local` 是本机配置，不入仓；`.vercelignore` 排除旧构建、测试截图和本机凭据。部署在 Vercel 重新运行 `build:demo`。

### 改用 Git 自动部署

1. GitHub 登录 Vercel，选择个人、非商业用途的免费 Hobby 计划。
2. 本批代码提交并推送后，Import Git Repository，选择 Sinan 仓库。
3. Root Directory 选择 `travel-frontend-react`，Framework Preset 选择 Vite，Node.js 选择 22.x。
4. 仓内 `vercel.json` 指定 `npm run build:demo` 和产物 `dist`，并将 SPA 路由重写到 `index.html`。不要选择根目录的 Python 服务。
5. 不填写数据库密码、JWT 或模型 Key，也无需购买域名。不要让项目环境变量覆盖 `.env.demo` 的 `VITE_DEMO_MODE=true`。
6. 点击 Deploy，使用生成的 Vercel 域名。验证首页、探索、我的行程、详情、编辑确认和刷新直达 `/trips/101`。

发布成功后仍须验证公开 URL 和目标网络体验；构建通过本身不能说明浏览器交互或外部地图正常。

Vercel 官方说明：[Hobby 计划](https://vercel.com/docs/plans/hobby)、[Vite 与 SPA 路由](https://vercel.com/docs/frameworks/frontend/vite)。
