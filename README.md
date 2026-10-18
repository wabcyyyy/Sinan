# 司南 Sinan

一句话描述旅行想法，生成每日行程，再用对话提出调整、查看差异并确认。
司南使用 React + FastAPI + LangGraph，提供逐日交付、SSE 进度、地图深链与中文 PDF 导出。

[English](README.en.md) · [架构](docs/public/architecture.md) · [开发](docs/public/development.md) · [参与贡献](.github/CONTRIBUTING.md)

小规模上线使用 [Railway 部署指南](docs/public/production.md)：同域整站容器、邀请码、一次性 6 元试用赠额与最小管理后台。仓库提供部署准备，公网与真实模型验收状态须单独确认；不依赖平台的自托管（Caddy 全栈）见 [deploy/README.md](deploy/README.md)。

个人作品展示可使用 [Vercel 免费展示版](docs/public/vercel-demo.md)：复用完整正式前端页面，以本地样例体验规划、地图与编辑确认，无需后端或模型 Key。

## 先体验：不需要模型密钥

准备 Docker Engine/Desktop 与 Compose 2.20+，在项目根目录执行：

```bash
docker compose -f compose.setup.yml run --rm setup
docker compose --profile apps up -d --build --wait --wait-timeout 300
```

打开 [完整页面演示](http://localhost:5173/demo)，或访问 [Vercel 展示站](https://sinan-demo.vercel.app)。无需登录，复用新版首页、探索、行程列表、详情、地图与编辑确认，以成都两天一晚固定样例体验交互。部署方式见 [Vercel 展示说明](docs/public/vercel-demo.md)。

**这是合成固定示例，不是实时 AI 生成。** 演示不调用业务 API 或模型；交互数据只保存在当前标签页，刷新可恢复，重置可清除，不证明模型质量。
容器首次构建需要下载镜像和依赖；地图底图需要联网，失败时仍能查看行程与地图深链。

初始化从模板生成随机口令、JWT 与内部令牌，默认模型 key 留空。已有配置与数据不会被覆盖。
启动失败先看 [排障指南](docs/public/troubleshooting.md)，运行：

```bash
docker compose exec -T agent-python python scripts/preview_doctor.py
```

## 真实规划能做什么

- 对话收集目的地、天数、人数、偏好与意图。
- 生成每日安排与预算，实时展示交付进度及失败或待核实状态。
- 对话提出编辑建议，查看变化后确认；通过地图深链核实地点。
- 出发前自动检查天气风险并给出可确认的调整草稿；旅行偏好可显式保存与遗忘，见 [照看与记忆](docs/public/care-and-memory.md)。
- 管理个人行程，提供分享与中文 PDF 导出。

真实生成需要在后端 `.env` 配置模型网关、模型名与 key，详见 [配置指南](docs/public/configuration.md)。
模型调用消耗你的额度，外部地点服务也有可用性与配额限制。
费用、开放时间、坐标等可能是估算；出发前应核实，生成结果不等于实时旅行事实。

## 工程与证据

后端只有一个 FastAPI 进程，业务经服务层调用 Agent 门面。跨端 schema 为单一源，迁移只追加。
CI 检查类型、分层、契约漂移、离线测试、评测棘轮、前端外观，以及全新 Compose 启动和固定回放。
固定回放验证交互，mock 评测验证约束与失败路径；真实模型报告须单独说明输入集、配置和样本量。
测试通过不代表任意模型或城市都能稳定生成优质行程。

## 本地开发与贡献

开发环境为 Python 3.12 + uv、Node 22.12+；CI 与容器使用 Node 22，依赖由锁文件安装。
启动、门禁和字体维护命令见 [开发指南](docs/public/development.md)。
欢迎可复现 Bug、文档修正与小范围改进，流程见 [贡献指南](.github/CONTRIBUTING.md)。

## 许可与安全

自身代码与文档使用 [MIT](LICENSE)。图片、字体与依赖使用各自许可，详见 [第三方资源](THIRD_PARTY_NOTICES.md)。
安全漏洞请按 [安全政策](.github/SECURITY.md) 私密报告，不要在公开 Issue 放置漏洞细节或凭据。
当前为开源预览阶段，尚无长期支持版本承诺。版本化镜像与正式 Release 不属于本批交付。
