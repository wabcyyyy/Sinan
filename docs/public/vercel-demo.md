# Vercel 个人展示版

展示构建复用正式 HomePage、ExplorePage、TripsPage、TripDetailPage、ChatPanel 和 MapLibre 页面。它不使用精简演示页面，也不连接 FastAPI、数据库或真实模型。

## 展示内容

- 首页“90秒体验局部修改”进入可跳过的三步引导：看第二天、发送固定调整指令、核对提案后保存。每次进入创建独立合成副本，保留已有演示编辑；正式模式免登录入口为 `/demo?tour=edit`。真实模型运行案例通过独立链接查看，不把固定样例当作模型实跑。
- 详情页桌面端时间线与工具栏并排，右侧切换地图、对话和待确认修改；手机端切换行程／地图／对话／修改预览，保留当前日期和草稿。
- 修改预览在原时间线上标记待移除、待调整条目，右侧展示提案；未确认时原行程与地图不变。“暂不应用”只收起预览，建议仍可重新打开，不表示服务端已撤销草稿。
- 费用变化仅比较提案涉及日期中有价格、可比较的人民币条目；按同行人数计算非住宿小计。缺价、外币或住宿变化时显示待核对，不推算节省额。保留说明仅覆盖提案未涉及的日期与住宿，不把部分字段相同当成所有内容都相同。
- 首页对话与信息确认、固定成都两人两天一晚的逐日交付、完成预览。
- 探索页的目的地、灵感与攻略，以及个人行程列表。
- 详情页每日时间线、地图点位与导航深链、预算、收藏、标题编辑。
- 对话输入“把第二天安排松一点”，先查看差异卡，确认后移除第二天的杜甫草堂；两人条目费用小计 1180 → 1080 元，第一天和住宿不变。小计不含未列出的交通费，所有单价均为样例估算。
- 演示行程和对话保存在当前标签页的 sessionStorage，刷新可续；重置体验恢复内置样例。与真实账号身份、草稿键隔离。

页面持续显示“产品交互演示 / 合成样例 / 不调用 AI”。其他城市的实时生成、其他自然语言编辑、分享、PDF、图片识别、语音转写和模型密钥配置需要真实后端；展示版不执行这些操作，不把固定结果冒充模型输出。地图底图仍依赖外网，失败时显示地图错误提示，行程和导航深链可继续使用。

2026-10-22 本地浏览器验收：1440×1000 桌面并排核对、390×844 手机视图切换、暂不应用后重开、确认后刷新保留、第一天与住宿保留、重复引导创建独立副本、跳过引导均已走通。使用固定合成数据，没有调用模型；这些截图不证明真实模型规划质量。自动回归沿用 `e2e/demo-path.mjs`，覆盖地图失败与零业务 API 请求。

[桌面修改预览](preview/workbench-edit-desktop-20261022.png) · [手机修改预览](preview/workbench-edit-mobile-20261022.png)

## 本地预览

在 `travel-frontend-react` 运行：

```bash
npm ci
npm run build:demo
npm run preview
```

打开终端给出的地址，首页就是完整展示版。常规 `npm run dev` / `npm run build` 保留真实后端模式；其中 `/demo` 及 `/demo/trips/101` 是相同完整页面的隔离演示入口。

## 发布到 Vercel

公开展示站：[sinan-demo.vercel.app](https://sinan-demo.vercel.app)。部署自己的实例时，在 Vercel 中导入你的 Sinan Fork，并使用下方构建设置。

2026-10-21 已更新静态展示，部署 `dpl_9xozUmpoHvEyrHXVZNdG8tATbYZv`。本次在本地执行 `build:demo`，仅上传静态构建产物；没有上传本地数据库、环境文件或源码工作树。公网首页、详情直达与刷新、390×844 手机视口已检查，最终版本的匿名直达请求返回 200 与新构建入口。此记录描述当次静态发布；后续源码提交与 CI 须按各自版本核对，真实模型案例及限制见 [产品案例](product-case-20261021.md)。

该发布方式采用 Vercel 的 [Build Output API](https://vercel.com/docs/build-output-api) 与 [prebuilt 部署](https://vercel.com/docs/cli/build)，可以单独上传构建产物。日常源码发布仍按下方 Git 集成流程执行。

### 自动发布更新

启用 Git 集成并选择生产分支后，将通过验证的修改推送到该分支即可触发构建，成功后更新你的展示站。其他分支可用于预览，无需上传本地构建目录。

Vercel 项目设置：

| 设置 | 值 |
| --- | --- |
| Git repository | 你的 Sinan Fork |
| Production branch | 你的发布分支（默认 `master`） |
| Root Directory | `travel-frontend-react` |
| Framework | Vite |
| Build Command | `npm run build:demo` |
| Output Directory | `dist` |

`build:demo` 读取入仓的 `.env.demo`，其中仅含公开开关 `VITE_DEMO_MODE=true`。不需要数据库密码、JWT 或模型 Key，也无需购买域名。`.vercel/` 与 `.env.local` 是本机配置，不入仓。

在自己的 Vercel 项目部署列表中查看对应 Git 提交的状态。失败时检查日志，站点继续使用上一版成功部署；需要重试时在部署详情中使用 Redeploy。Git 集成需保留对所选仓库的授权。

发布成功后仍须验证公开 URL 和目标网络体验；构建通过本身不能说明浏览器交互或外部地图正常。

Vercel 官方说明：[Hobby 计划](https://vercel.com/docs/plans/hobby)、[Vite 与 SPA 路由](https://vercel.com/docs/frameworks/frontend/vite)。
