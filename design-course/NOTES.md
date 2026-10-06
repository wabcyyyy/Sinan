# NOTES

教学偏好与 working notes（改课程设计前先读这里）。

- 用户：应届生、单人 vibe-coder、中文交流；实战优先、反企业级冗余、预算敏感（付费资源先给免费平替）
- 教学定位修正（2026-09-29，开课时）：用户**不缺设计文档**（已有 563 行 TREK-DESIGN-STYLE.md），缺的是把文档变成日常 AI 迭代纪律的**流程**。因此第一课从"盲眼工匠 + 形容词无信息量"切入，而不是从"什么是设计系统"切入。
- 已验证的工程家底：TREK-DESIGN-STYLE.md、frontend-design 官方技能（`.agents/skills/frontend-design/`）、pixelmatch 对照工具链（TREK 复刻时建的）、前端 theme:lint 门禁
- 用户历史结论（尊重，勿反复推销）：design-taste-frontend 技能不适用产品型 UI（偏 landing page / 作品集）；司南走 Linear/TREK 式克制路线
- 曾用 Astra（AI 建站工具）做前端仍不满意 —— 佐证"换工具不解决流程问题"，是课程立论的实证素材
- 工作区选址：放在仓库 `design-course/` 子目录（teach 技能默认用当前目录，但仓库根已有项目文件，避免混入项目结构；用户可整体移走或 gitignore，不影响课程）
- **流程偏好修正（2026-09-29 第二次反馈，课程转向）**：用户时间极少、主要靠直接生成，拒绝高用户成本的流程。批评/翻译工作全部转给 AI，用户只保留"过 / 不过"。lesson 0001 的五问作业降级为可选，五问卡改为 AI 自查清单。0002 起 = 快通道路线：一页约束（`reference/sinan-style-brief.md`）+ 一句话提示词包（`reference/one-line-prompt-kit.html`）+ AI 自评闭环。lesson 时长也要短（0002 压到 5 分钟、无作业）——课程自身必须示范"尊重时间"。
- 待办：用户同意后，可把 `sinan-style-brief.md` 接进 `travel-frontend-react/AGENTS.md`，让约束在每个前端会话自动生效（真正零日常成本）——需要用户拍板，因为改的是项目级 AGENTS
