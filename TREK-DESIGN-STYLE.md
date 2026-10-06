# TREK 4.2.1 前端设计风格完整描述

> 来源：`D:\wcy\project\TREK-4.2.1\client\`（React + Vite + Tailwind）
> 用途：作为 Travel-Assistant 前端改版的设计参照，可直接映射到 token / 组件 / 布局决策。
> 版本锚点：TREK 4.2.1

---

## 0. 一句话气质

**「冷静、可定制的工具感 + 关键时刻的液态玻璃（liquid glass）」**——  
默认几乎是单色黑白（近黑 accent / 近白 accent），靠字体层级、圆角卡片、轻投影和克制的玻璃模糊建立质感；在 Dashboard / Vacay 等「旅程叙事」场景，再叠一层 Apple 式毛玻璃与大数字排版，做出旅行产品的记忆点。

它不是「彩色 SaaS 模板」，而是 **self-hosted 协作工具** 的高级皮肤：默认 monochrome，用户可一键换成靛蓝 / 青绿 / 玫瑰 / 琥珀 / 紫罗兰，甚至自定义 accent。

---

## 1. 视觉气质与设计原则

| 原则 | TREK 的做法 |
| --- | --- |
| **Token 单一真源** | 所有颜色 / 字号层级 / 阴影 / 圆角走 CSS 自定义属性；组件只读 token，从不读用户配置算样式 |
| **语义化而非色号** | 工具类用 `bg-surface` / `text-content` / `border-edge` / `bg-accent`，禁止 `bg-slate-900`、`bg-white` 当语义 |
| **主题可插拔** | light/dark + 8 种 scheme + 自定义 accent + 紧凑密度 + 关闭透明度 + 减弱动效，全部可用户级覆盖 |
| **双端同魂** | 桌面（业务工具）与手机（Travel journal / sheet）共用精神，但 token 命名不同（`--bg-*` vs `--m-*`） |
| **动效有 punch** | 全局把 Tailwind 默认 easing 换成 `ease-out-quint`，按钮 press 有缩放反馈，抽屉用 iOS 抽屉曲线 |
| **地图即画布** | 地图浮层（popup / tooltip / cluster）与应用 chrome 同一视觉语言（圆角、毛玻璃、token 色） |
| **空态有人格** | 单一 TREK 吉祥物（`MDancingTrek`）演不同 scene/mood，全站统一 empty state，不用通用插画 |

### 风格锚点（可感性参照）

- 产品感：Linear / Arc / Apple Maps 工具条 / Polarsteps 阅读流 的杂交
- 印刷感：瑞士式无衬线 + 大数字 editorial hero，而不是插画风
- 透明度：iOS Liquid Glass（Dashboard hero pass、Vacay card）+ 克制的 frosted navbar

---

## 2. 色彩系统（Palette）

### 2.1 架构

```
:root (light)  +  .dark (dark)  +  [data-scheme="…"]  +  [data-density]  +  [data-no-transparency]
     │                │                    │
     └────────────────┴────────────────────┘
                      ↓
         Tailwind 语义色映射（surface / content / edge / accent / status）
                      ↓
              组件 className（bg-surface-card 等）
```

### 2.2 默认 Light（monochrome 默认）

| Token | 值 | 用途 |
| --- | --- | --- |
| `--bg-primary` | `#ffffff` | 页面底 |
| `--bg-secondary` | `#f8fafc` | 次级面 / 区块 |
| `--bg-tertiary` | `#f1f5f9` | 更浅面 / 分段控件槽 |
| `--bg-elevated` | `rgba(250,250,250,0.82)` | 浮起毛玻璃面（sidebar/tooltip） |
| `--bg-card` | `#ffffff` | 卡片 / 弹层内容 |
| `--bg-input` | `#ffffff` | 表单 |
| `--bg-hover` | `rgba(0,0,0,0.03)` | 悬停 |
| `--bg-selected` | `#e2e8f0` | 选中 |
| `--text-primary` | `#111827` | 主文案（近黑，偏冷灰） |
| `--text-secondary` | `#374151` | 次文案 |
| `--text-muted` | `#6b7280` | 弱化 |
| `--text-faint` | `#9ca3af` | 极弱 / 地址 / 标签 |
| `--border-primary` | `#e5e7eb` | 主描边 |
| `--border-secondary` | `#f3f4f6` | 分割线 |
| `--border-faint` | `rgba(0,0,0,0.06)` | 极弱描边 |
| **`--accent`** | **`#111827`** | **主按钮填充（默认=近黑，不是品牌蓝！）** |
| `--accent-text` | `#ffffff` | 主按钮文字 |
| `--accent-hover` | `#1f2937` | 主按钮 hover |
| `--accent-subtle` | `#f1f5f9` | 淡强调底（chip/selected） |
| `--bg-inverse` / `--text-inverse` | `#111827` / `#ffffff` | 「黑药丸」标题条 |
| `--overlay` | `rgba(0,0,0,0.5)` | 模态遮罩 |

### 2.3 默认 Dark

| Token | 值 |
| --- | --- |
| `--bg-primary` | `#121215` |
| `--bg-secondary` | `#1a1a1e` |
| `--bg-tertiary` | `#1c1c21` |
| `--bg-elevated` | `rgba(19,19,22,0.82)` |
| `--bg-card` | `#131316` |
| `--bg-input` | `#1c1c21` |
| `--bg-hover` | `rgba(255,255,255,0.06)` |
| `--bg-selected` | `rgba(255,255,255,0.1)` |
| `--text-primary` | `#f4f4f5` |
| `--text-secondary` | `#d4d4d8` |
| `--text-muted` | `#a1a1aa` |
| `--text-faint` | `#71717a` |
| `--border-primary` | `#27272a` |
| `--border-secondary` | `#1c1c21` |
| `--border-faint` | `rgba(255,255,255,0.07)` |
| **`--accent`** | **`#e4e4e7`**（反转：浅填充 + 深字） |
| `--accent-text` | `#09090b` |
| `--bg-inverse` / `--text-inverse` | `#e4e4e7` / `#09090b` |

暗色不是「把 light 灰度翻转」，而是 **锌灰（zinc）+ 近黑卡片 + 浅色主按钮**。

### 2.4 Status

| 语义 | Light | Soft | Dark | Soft |
| --- | --- | --- | --- | --- |
| success | `#16a34a` | `#dcfce7` | `#22c55e` | `rgba(34,197,94,.15)` |
| danger | `#dc2626` | `#fef2f2` | `#ef4444` | `rgba(239,68,68,.15)` |
| warning | `#d97706` | `#fffbeb` | `#f59e0b` | `rgba(245,158,11,.15)` |
| info | `#2563eb` | `#eff6ff` | `#3b82f6` | `rgba(59,130,246,.15)` |

### 2.5 Color Schemes（`data-scheme`）

默认 scheme **不写属性**，保持 monochrome。其他 scheme 只覆盖 accent 家族：

| ID | Light accent | Dark accent | 备注 |
| --- | --- | --- | --- |
| `default` | `#111827` | `#e4e4e7` | 单色 |
| `highContrast` | `#1d4ed8` | `#60a5fa` | 同时抬高中性对比、去透明 |
| `indigo` | `#4f46e5` | `#6366f1` | Tailwind indigo 族 |
| `teal` | `#0d9488` | `#14b8a6` | |
| `rose` | `#e11d48` | `#f43f5e` | |
| `amber` | `#d97706` | `#f59e0b` | |
| `violet` | `#7c3aed` | `#8b5cf6` | |
| `custom` | 用户色 | 用户色（可分 light/dark） | `color-mix` 派生 hover/subtle |

自定义 accent 预设 10 色：`#4f46e5 #0d9488 #e11d48 #d97706 #7c3aed #2563eb #db2777 #059669 #ea580c #0891b2`。

### 2.6 领域子调色

- **Journey**：`--journal-*`（近 zinc）+ mood 色（amazing 珊瑚 / good 琥珀 / neutral 灰 / tired 蓝 / rough 紫）
- **Vacay**：`--vg-*`（OKLCH 液态玻璃）
- **Planner**：固定 slate 系（`day: #f8fafc` 等，迁移中的遗留）
- **Mobile**：独立 `--m-*` 暖灰纸感 / 冷黑，见 §7

---

## 3. 字体与排版（Typography）

### 3.1 字体

| 角色 | 字体栈 | 字重习惯 |
| --- | --- | --- |
| 系统 / 标题 / 正文 / 数字 | **Poppins**（本地 TTF：Regular/Medium/SemiBold/Bold/Italic）→ 系统 UI | 400 正文，500 控件，600 标题/数字，700 特大日期 |
| 副文案 / caption / Geist 层级 | **Geist Sans** → Poppins → 系统 | 500–600；类 `text-content-faint` 强制切到 Geist |

设计口诀（源码注释原话）：**「Geist text · Poppins numbers」**——说明/标签用 Geist，数字/标题用 Poppins。

### 3.2 语义字号层级（可用户缩放）

| Utility | 基准 | line-height | 典型用途 |
| --- | --- | --- | --- |
| `text-title` | `24px × --fs-scale-title` | 1.2 | 页面/区块大标题 |
| `text-subtitle` | `18px × --fs-scale-subtitle` | 1.35 | 小节标题 |
| `text-body` | `14px × --fs-scale-body` | 1.5 | 正文、表单 |
| `text-caption` | `12px × --fs-scale-caption` | 1.4 | 地址、标签、元信息 |

另有全局 `fontScale` 写在 `html { font-size: N% }`，与四档倍率相乘。用户可 80%–160%（步进 5%）。

### 3.3 Editorial 大数字排版（Dashboard / Journey 特有）

| 元素 | 规格 |
| --- | --- |
| Hello 大标题 | 56px / 600 / ls `-0.035em` / lh 1.02 |
| Hero 标题（封面） | 104px / 600 / ls `-0.045em` / lh 0.9 + 文字阴影 |
| 大数字 pass | 32–44px / 600 / 紧字距 + 小单位 16px muted |
| 日期数字 | 36px / 700 / ls `-0.03em` |
| 小节标题 | 28px / 600 / ls `-0.025em` |
| Eyebrow / label | 10.5–12px / 500 / **uppercase / ls 0.1–0.22em** |
| Chip / badge | 11.5–13px / 500–600 |

**排版签名**：超大负字距标题 + 宽字距全大写小标签 + 中号数字单位后缀。这是 TREK 比一般工具 UI 更「旅行杂志」的地方。

### 3.4 常规控件字号

- 输入框 / 小按钮：13px
- 表单标签：caption 层级 + uppercase + tracking 0.14em + `font-semibold`
- Modal 标题：`text-lg font-semibold`（约 18px）
- 空态标题：15px / 600 / secondary

---

## 4. 间距、圆角、阴影、层次

### 4.1 间距节奏

```
--sp-1: 4px    --sp-2: 8px    --sp-3: 12px
--sp-4: 16px   --sp-6: 24px
```

Compact 密度（`data-density="compact"`）整体收紧到 3/6/9/12/18px。

常用内边距手感：卡片 `24–26px`，表单输入 `8px 14px`（compact `5px 11px`），按钮 `10–12px × 18–20px`，Modal 头身 `24px`。

### 4.2 圆角体系

| Token | 值 | 用途 |
| --- | --- | --- |
| `--radius-sm` | 8px | 小按钮、seg 内钮、tooltip |
| `--radius-md` | 10–12px | 输入框、chip 容器、菜单 |
| `--radius-lg` | 14–16px | 卡片、弹层、面板 |
| `--radius-xl` | 20–24px | Modal、玻璃大卡、hero pass |
| `999px` / `50%` | pill / circle | 状态条、头像、meta chip、状态点 |

**签名圆角**：业务卡片 12–16px，大浮层 20–24px，pill 全圆。没有 4px 级「SaaS 硬角」。

### 4.3 阴影阶梯

| Token | Light | 角色 |
| --- | --- | --- |
| `--shadow-sm` / card | `0 1px 2px rgba(0,0,0,.05)` / 双层轻影 | 卡片 |
| `--shadow-md` / elevated | `0 4px 12px…` / `0 4px 16px…` | 浮起面 |
| `--shadow-lg` | `0 12px 32px rgba(0,0,0,.12)` | 悬浮 |
| `--shadow-dropdown` | `0 8px 24px…` | 下拉 |
| `--shadow-popover` | `0 8px 32px…` | 地图 popup |
| `--shadow-modal` | `0 16px 48px rgba(0,0,0,.20)` | 模态 |
| Glass | `0 1px 2px… + 0 12px 32px -14px…` + inset 高光 | 液态玻璃 |

Dark 下阴影透明度整体加深（0.3–0.6）。

### 4.4 层叠 z-index 尺度（必守）

| Token | 值 | 对象 |
| --- | --- | --- |
| `--z-bar` | 60 | BottomNav / sticky bar |
| `--z-nav` | 200 | 桌面 Navbar |
| `--z-modal` | 10000 | Modal / Confirm |
| `--z-overlay` | 99990 | 页面全屏覆盖 |
| `--z-notice` | 99998 | 系统通知 |
| `--z-toast` | 100000 | Toast / tooltip / 右键菜单 |

禁止自造 z 数值。

---

## 5. 动效与微交互（Motion）

### 5.1 Easing

| Token | 曲线 | 用途 |
| --- | --- | --- |
| `--ease-out-quint` | `cubic-bezier(0.23, 1, 0.32, 1)` | **全局默认**（替换 Tailwind ease） |
| `--ease-in-out-quint` | `cubic-bezier(0.77, 0, 0.175, 1)` | 强对比过渡 |
| `--ease-drawer` | `cubic-bezier(0.32, 0.72, 0, 1)` | 移动端 sheet/drawer |

### 5.2 时长与动画

| 场景 | 时长 | 曲线 | 备注 |
| --- | --- | --- | --- |
| 按钮 hover/transition | 150–180ms | out-quint | `transition-property` 全局统一 |
| 按钮 press | 80ms | scale ~0.97 | 有按压手感 |
| Input focus | 150ms | out-quint | border + ring |
| Menu enter | 200ms | out-quint | |
| Popover / map popup | 180–220ms | out-quint | origin bottom center |
| Modal enter | 220ms | out-quint | |
| Backdrop | 180ms | out-quint | |
| Toast enter | 260ms | out-quint | |
| Drawer | 320ms | drawer 曲线 | |
| Mobile sheet in/out | 280ms | drawer | |
| Page enter | 220ms | out-quint | |
| Fade-up | 280ms | out-quint | |
| Theme 切换 | 320ms | out-quint | 全局 `trek-theme-transitioning` |
| Navbar 背景 | 240ms | out-quint | 随滚动加深 blur/shadow |
| Bar fill / pie reveal | 700 / 900ms | out-quint | 图表入场 |
| Skeleton | shimmer | | 禁动效时静态 `--bg-tertiary` |

### 5.3 手感签名

1. **Press scale**：几乎所有 button/role=button 按下缩放，抬起弹回。
2. **Ease-out-quint everywhere**：比默认 ease「更有力地落定」。
3. **主题 320ms 全局色彩过渡**（图片/视频/canvas 除外）。
4. **`data-reduce-motion`**：关闭 parallax、lift、press transform、复杂进入动画——无障碍是产品特性而非补丁。

---

## 6. 组件设计语言

### 6.1 按钮

| 类型 | 视觉 |
| --- | --- |
| **Primary** | `bg-accent` + `text-accent-text` + `rounded-xl` + `shadow-card`，px-5 py-2.5，14px medium |
| **Ghost / 次按钮** | 描边 `border-edge` + `text-content-secondary` + `rounded-xl`，hover `bg-surface-hover` |
| **Dashed 创建** | `border-dashed border-edge`，用于「新建分享链接 / 空创建」 |
| **Icon button** | `p-1.5–2` + `rounded-lg`，hover 面 |
| **Pill / seg** | 背景槽 + 内嵌圆角钮（密度 3px padding） |
| **FAB / add circle** | 48px 圆，fill 用 accent |

**硬规则**：主操作永远 `bg-accent`，绝不用 `bg-slate-900` / `bg-indigo-*` 写死。

### 6.2 卡片

- 底：`bg-surface-card` 或 **glass**（`--glass-bg` 渐变 + blur 22px saturate 1.7 + 1px 半透明描边 + 双层影 + `inset 0 1px 0` 高光）
- 圆角：`rounded-xl`（12）~ `rounded-2xl`（16）~ hero `24px`
- Hover：阴影升到 `glass-shadow-hover`，可选轻微 translate
- 封面图卡：图片铺满 + 底部黑色渐变压字 + 状态 pill 玻璃

### 6.3 Modal

- 遮罩：`rgba(15,23,42,0.5)` ≈ `--overlay`
- 面板：`rounded-2xl` + `shadow-2xl` + `bg-surface-card`
- 头：`p-6` + 底边 `border-edge-secondary` + `text-lg font-semibold` + 右上 X icon
- 体：`p-6` 可滚动
- 尺寸阶梯：`sm → 5xl`（max-w-sm … max-w-7xl）
- 交互：Esc 关、点遮罩关、body scroll lock 引用计数

### 6.4 表单

- `.form-input`：border-primary，`border-radius: 10px`，`8px 14px`，13px，`--bg-input`
- Focus：border → text-faint（或 accent ring `focus:ring-accent/30 focus:border-accent`）
- 标签：caption + uppercase + tracking + semibold + faint
- 错误块：`bg-danger-soft border-danger/30 rounded-xl text-danger`

### 6.5 Toast

- 图标：18px lucide，色 success `#22c55e` / error `#ef4444` / warning `#f59e0b` / info `#6366f1`
- 进入 260ms，存在 3s 默认
- 位置固定高 z（`--z-toast`）

### 6.6 Empty State

- **唯一模式**：TREK 吉祥物（SVG 编舞动画）+ 一行标题 + 可选 CTA
- 吉祥物单色，用 `--m-ink` / `--m-bg` 映射到桌面色板
- 竖排居中 `px-6 py-12`；矮面板用横排

### 6.7 导航

| 端 | 形态 |
| --- | --- |
| 桌面 Navbar | 固定顶栏 64px + safe-area；**毛玻璃**（blur 20–28px + saturate 180%）；滚动后更实、阴影更深；左返回/品牌，中 trip title pill，右动作/主题/用户 |
| 手机 TopBar + BottomNav | BottomNav 高 `84px + safe-area`，`--z-bar`；图标+短标签；可配置固定项 |
| Sidebar（planner） | 毛玻璃 `--sidebar-bg` + `--sidebar-shadow` |

### 6.8 地图浮层

- Popup：`border-radius 14px`，`bg-card`，`border-faint`，`shadow-popover`，enter 从 anchor 弹出
- Hover 标签：白/玻璃小卡 10px 圆角，无尾巴（「标签」而非「气泡」）
- Cluster：深色圆 + 白描边 2.5px + 数字 Poppins 700，hover scale 1.1
- Zoom 控件：暗色玻璃；手机隐藏系统 zoom 控件

### 6.9 滚动条

- 桌面：6px 细条，圆角 3px，track/thumb/hover 三档 token
- 手机：隐藏
- 阅读流（Journey）：`scrollbar-width: none`，保持 Polarsteps 式沉浸

---

## 7. 布局系统

### 7.1 桌面

```
┌─────────────────────────────────────────────┐
│ Navbar (fixed, glass, 64px)                  │
├──────────────┬──────────────────────────────┤
│ Sidebar /    │  Content (paddingTop: --nav-h)│
│ Day column   │  或 map + feed 双栏           │
│ (optional)   │                              │
└──────────────┴──────────────────────────────┘
```

- `PageShell`：全高主题根 + Navbar + 内容偏移，统一 chrome
- Dashboard：editorial hero + 玻璃 pass 卡 + 网格 trip 卡 + 可选右侧 widgets
- Planner：左日程列表 / 右地图，日列卡片阴影 `day-column`
- 移动断点：`md: 768px`（Navbar 出现）；`<768px` 走 MobileShell

### 7.2 手机（独立设计系统）

手机不是「响应式压扁桌面」，而是 **第二套 token + sheet 布局**：

| Token 族 | 气质 |
| --- | --- |
| `--m-bg` / `--m-scr` | 暖纸灰 `#F3F2EF` + 径向渐变光；暗色近黑 `#0A0A0C` |
| `--m-ink/muted/faint` | `#101013 / #68686F / #9A9AA1`（暖中性） |
| `--m-glass/card/inner` | 半透明白玻璃三层 |
| `--m-sheet*` | 底部 sheet `rgba(250,250,248,.94)` |
| `--m-act/actfg` | 主操作仍是近黑填充 |
| `--m-st-*` | 状态色独立（confirmed 绿 / pending 琥珀 / info 蓝 / danger 红） |

- 布局：TopBar + 可滚动内容 + BottomNav；交互以 **sheet / drawer** 为主（280ms drawer 曲线）
- 字体：`font-geist` 作 subtext；数字仍 Poppins
- 触控：reorder 钮改为始终可见、32px 方钮；无 hover 依赖
- 范围：`--m-*` 仅在 `.m-root` / MobileShell 内有效

### 7.3 Vacay「Liquid Glass」子系统

独立 `--vg-*`（OKLCH），`.vg-card` 组合：

```css
background: linear-gradient(135deg, oklch(1 0 0 / .72), oklch(0.99 0.006 75 / .5));
border: 1px solid oklch(0.88 0.008 70 / .7);
backdrop-filter: blur(22px) saturate(1.7);
box-shadow: 多层软影 + inset 高光;
```

用于假期日历 / 玻璃面板，是全站最「苹果」的表面。

### 7.4 响应式与安全区

- `env(safe-area-inset-*)` 贯穿 nav / bottom nav
- 手机 `html/body` 滚动策略特殊处理（iOS 地址栏 / PWA 下拉刷新 / overflow-x clip）
- 按 `pointer: coarse` 分支触控行为（平板宽屏无 hover）

---

## 8. 可定制性（Appearance Platform）

用户设置（实时预览、账号级持久化）：

1. **Color mode**：Light / Dark / Auto（跟系统）
2. **Color scheme**：8 套 + Custom（双端可不同色 + 对比度徽章 WCAG）
3. **Readability**（Experimental）  
   - Transparency 开关：关闭时把 alpha 面变实色、去 blur  
   - Reduce motion  
   - Density：Comfortable / Compact
4. **Text size**：Everything + Large/Medium/Normal/Small 四轴 80–160%
5. **Dashboard widgets** 桌面/手机独立布局
6. **Mobile nav** 可配置 BottomNav 项

实现约束（给 Travel-Assistant 的架构启示）：

- 配置 → `applyAppearance()` **唯一** 写 DOM（class / data-* / CSS vars）
- `theme-boot.js` 同步脚本防 FOUC
- 组件永不读 AppearanceConfig 算样式，只读 token
- `theme:lint` 机器执法：禁裸 slate/white、禁 inline fontSize、禁 JSX 内 rgba glass

---

## 9. 地图视觉

- 引擎：Leaflet / Mapbox GL / MapLibre GL（OpenFreeMap 免 token）
- Marker：自定义 cluster + 拖拽 ghost（dragging 透明度 0.4）
- Route：轨迹色表 `trackColors`；航班大圆线
- 控件：与 chrome 同语言（圆角、玻璃、token）
- 高德/百度那种「重彩色 POI 图标」**不是** TREK 风格——POI 克制，行程本身才是主角

---

## 10. 图标与插画

| 类型 | 约定 |
| --- | --- |
| 功能图标 | **lucide-react**，线性、1.5–2px 描边，尺寸 12–22px |
| 空态/加载 | TREK 吉祥物 SVG（多 scene/mood，编舞 class `.trek-*`） |
| Logo | 双色 SVG（light/dark），字标 + 图形；禁选中/拖拽 |
| 品牌图 | 无水印旅行摄影为主，压深色渐变字 |

**不用**：emoji 当图标、彩色 filled 拟物 icon、通用 undraw 插画。

---

## 11. 动效细节补充

- 条形图/饼图入场：`trek-bar-fill` 700ms / `trek-pie-reveal` 900ms
- 页面容器 `trek-page-enter` 220ms fade/slide-up
- 吉祥物：`trek-shadow` 3.4s 循环微动
- 拖拽 overlay：`shadow-drag-overlay` 大影
- 按钮 loading：16px 双环 spinner（border 2px，accent-text 30% 轨）

---

## 12. 可访问性与稳健性

- `:focus-visible` 统一 outline：`2px solid var(--accent)`
- 语义字号四档 + 全局缩放，尊重用户
- High Contrast scheme 抬对比、去透明
- Reduce motion 关闭非必要动效
- 自定义 accent 提供对比度比（仅提示，不强拦）
- 主题预绘制 boot，避免闪白/闪黑
- 滚动锁引用计数，多层 overlay 不互相踩

---

## 13. 技术栈与样式实现方式

| 项 | TREK 选择 |
| --- | --- |
| 框架 | React + Vite + TS |
| 样式 | Tailwind 3（class 语义色映射 CSS vars）+ 少量页级 CSS（dashboard/collections/studio） |
| 字体 | 本地 Poppins TTF + Geist Sans |
| 图标 | lucide-react |
| 地图 | Leaflet / MapLibre / Mapbox |
| 状态 | zustand |
| 主题 | CSS variables + `data-scheme` + `.dark` |
| 防 FOUC | 阻塞式 `theme-boot.js` |

页级大 CSS（如 dashboard）仍用 OKLCH glass token，说明 **玻璃是「特征面」局部强化**，不是全站每像素都模糊。

---

## 14. 组件设计模式速查（可直接抄的 token 组合）

```text
主按钮   : bg-accent text-accent-text rounded-xl shadow-card px-5 py-2.5 text-body font-medium
次按钮   : border border-edge text-content-secondary rounded-xl hover:bg-surface-hover px-4 py-2.5
输入框   : bg-surface-input border border-edge rounded-xl px-3.5 py-2.5 text-body
           focus:ring-2 focus:ring-accent/30 focus:border-accent
标签     : text-caption font-semibold uppercase tracking-[0.14em] text-content-faint
卡片     : bg-surface-card border border-edge rounded-xl shadow-card
玻璃卡   : (token) blur(22px) saturate(1.7) + 1px glass border + inset highlight
状态 chip: bg-{status}-soft text-{status} rounded-full px-2.5 py-1 text-caption font-medium
Modal    : bg-surface-card rounded-2xl shadow-modal / shadow-2xl
错误块   : bg-danger-soft border border-danger/30 rounded-xl text-body text-danger
Eyebrow  : uppercase tracking-[0.16em] text-caption text-content-faint font-medium
大数字   : font-semibold tracking-tight + 小单位 text-content-faint
```

---

## 15. 与 Travel-Assistant 的映射建议（后续改版用）

| TREK 概念 | Travel-Assistant 建议落点 |
| --- | --- |
| Token 三层（CSS var → Tailwind 语义色） | 在 Vue 主题里建 `surface/content/edge/accent` 变量族，替换散落 hex |
| 默认 monochrome + 可选 scheme | 先做 light/dark 单色近黑主按钮，再考虑 accent 方案 |
| Poppins + 字号四档 | 中文场景改为「思源黑体/Noto Sans SC + 数字用 Poppins/Geist」或保留双字体层级 |
| Glass 只用于特征面 | 行程结果 Hero、地图工具条、移动端 sheet 用玻璃；表单列表保持实色 |
| Press scale + ease-out-quint | 全局按钮交互统一，成本低、体感强 |
| z-index 尺度 | 一次性定义 `--z-bar/nav/modal/toast`，禁止魔法数 |
| Empty state 吉祥物 | 可换成 Travel-Assistant 自己的 IP 角色，但保持「一角色 + 一句标题」 |
| Mobile 独立 token | 手机行程详情用 sheet；可先做暖纸灰或冷黑一套 |
| theme:lint 思想 | 用 lint/stylelint 禁止裸色值，保证后续 scheme 可扩展 |

### 建议优先迁移顺序

1. **Token 底盘**（色板 light/dark + 字号 + 圆角 + 阴影 + z）
2. **按钮 / 输入 / 卡片 / Modal** 四件套对齐
3. **Navbar 毛玻璃 + 滚动加深**
4. **动效曲线与 press**
5. **Dashboard/行程结果 editorial 大数字与玻璃 hero**
6. **（可选）scheme / density / reduce-motion**

---

## 16. 关键源码索引

| 内容 | 路径 |
| --- | --- |
| 主题 token 与 scheme | `client/src/index.css`（`:root` / `.dark` / `[data-scheme]`） |
| Tailwind 语义映射 | `client/tailwind.config.js` |
| 主题写入器 | `client/src/theme/applyAppearance.ts` |
| Scheme 元数据 | `client/src/theme/schemes.ts` |
| 主题契约文档 | `client/src/theme/README.md` |
| 防 FOUC | `client/public/theme-boot.js` |
| 移动 token | `client/src/mobile/mobile.css` |
| Dashboard 玻璃 | `client/src/styles/dashboard.css` |
| Modal / Empty / Toast | `client/src/components/shared/` |
| Navbar / PageShell | `client/src/components/Layout/` |
| 外观设置说明 | `wiki/Appearance-Settings.md` |
| 展示图 | `docs/screenshots/showcase-*.webp` |

---

## 17. 设计一句话备忘（贴工位）

> **单色为骨，玻璃为肤，数字为声。**  
> 近黑/近白 accent 保证工具专业感；液态玻璃只点在旅程高光；Poppins 大数字与宽字距大写小标签负责「旅行杂志」的语气。一切颜色走 token，一切动效走 quint 曲线。

---

*本文档基于 TREK 4.2.1 源码静态阅读整理，供 Travel-Assistant 前端视觉升级作单一参照。*
