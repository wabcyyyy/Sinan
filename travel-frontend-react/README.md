# travel-frontend-react

React 18 + Vite + TypeScript 壳（`src/react-app/`）：一句话对话创建（澄清卡 + 槽位确认条）、生成期实时预览、行程列表、详情页对话编排 + 单日工作台。`src/` 下 Vue3 组件树为已退役遗留（Vue 组件树已整体删除）。

## 技术栈

- Vue 3.4 + Vite 5 + TypeScript（vue-tsc 类型检查）
- **自研交互控件层 `src/components/ui/`**（对话框/抽屉/菜单/弹层/分段/输入/toast/确认框…）；Element Plus 2.7 为**存量面**并渐进退役——`npm run ep:lint` 对 EP 用量强制**只减不增**（管理员端/生成页在允许清单内）
- Pinia + Vue Router + vuedraggable（拖拽排序与跨天拖入）
- MapLibre GL：**OpenFreeMap 免 key 在线矢量底图**（亮/暗随外观切换，署名随图），编号圆钉/逐日路线色/按天可见性
- Geist Sans 拉丁子集**自托管**（`public/fonts/`，中文走系统栈；首屏零第三方字体请求）
- ECharts 6（管理端图表，按需注册）

## 页面与路由

| 路由 | 页面 | 说明 |
| --- | --- | --- |
| `/login` | LoginView | 登录 / 注册 |
| `/generate` | GenerateView | 输入城市/天数/偏好/预算 → 生成行程 |
| `/trips` | TripsView | 行程封面墙（状态筛选 / 搜索 / 收藏 / 归档） |
| `/trips/:id` | TripDetailView | **三栏工作台（视口固定，两栏各自滚）**：左=面板头条（返回/标题/操作菜单）+ 天平铺日卡（内联提示卡 / 副标题 / 建议方案 / 优化路线 / 行内时间费用 / 站间「步行·车程 ≈ …」估算片）；中=地图铺底（编号钉 / 按天可见性 / 路线开关 / 走廊工具簇 / 贴底详情卡）；右=发现（搜索加点 / 全部·未排·已排 / 就近推荐 / 拖拽入天）；两栏 340/300 默认、可拖宽（200–520）可收起（挂耳钮；<1024 宽单栏三态）；选择态批量条 + 版本历史 + 导出 |
| `/atlas` | AtlasView | 旅程图鉴（OpenFreeMap 底图 + 去过国家高亮 + 城市钉） |
| `/s/:token` | ShareView | 公开只读分享页 |
| `/` | HomeView | 首页（Hero CTA） |
| `/admin` | admin/Admin* | 管理端（仪表盘 / 用户 / 行程 / Token / Agent 指标，需 admin 角色） |

> 点位导航：行程行与贴底详情卡带地图跳转外链（外部超链接，非 API 依赖）；就近推荐只用本地库内的真实坐标与距离。

## 启动

```bash
cp .env.example .env   # 空白模板：前端不需要任何第三方 key
npm install
npm run dev            # http://localhost:5173，/api 代理到 8000（FastAPI）
```

构建：`npm run build`（先跑 `vue-tsc --noEmit` 类型检查）。

## 门禁

```bash
npm run test:unit    # vitest（纯逻辑 + 组件行为；happy-dom）
npm run theme:lint   # 外观契约：禁裸色/野 z-index/!important；豁免表只准变短
npm run ep:lint      # 去 EP：产品面禁新增 Element Plus 引用，允许清单只减不增
```

## 环境变量

| 变量 | 说明 |
| --- | --- |
| （无） | 前端不需要任何 key：地图为 OpenFreeMap 免 key 在线矢量瓦片（署名随图），点位/封面图经服务端同源代理获取（图库 key 只在服务端） |
