# 启动与演示排障

先确认 Docker 引擎可访问，依次执行：

```bash
docker compose -f compose.setup.yml run --rm setup python travel-agent-python/scripts/preview_setup.py check --root /workspace
docker compose --profile apps config --quiet
docker compose --profile apps ps
docker compose exec -T agent-python python scripts/preview_doctor.py
```

诊断只检查配置、依赖、前端与代理，不调用模型或打印环境值。

| 症状 | 排查 |
| --- | --- |
| Docker 无法连接 | 启动 Docker Desktop 或检查 daemon/socket 权限 |
| 初始化失败 | 检查模板完整性、目录可写性、env 编码和 data 权限 |
| 端口冲突 | MySQL/Redis 与应用端口的配置位置不同，见下方；不要停止其他项目的服务 |
| ta-* 容器名冲突 | 已有本项目实例继续使用原实例；隔离验证需独立目录、项目名和容器名 |
| 后端启动拒绝 | JWT/内部令牌可能仍为占位符，检查 env 覆盖顺序 |
| Redis 失败 | 检查口令与是否显式填写错误 REDIS_URL |
| 上传/PDF 不可写 | 容器 UID 1000；人工检查 data 权限后决定调整，不要递归改已有数据 |
| `/demo` 刷新 404 | 静态服务器需要 SPA fallback，项目的 Vite/nginx 已配置 |
| 地图留白 | 需要联网与 WebGL；列表和深链仍可用 |
| 真实生成失败 | 回放不调用模型；检查 provider、模型、key、配额和网络 |

重新初始化不会替换现有 env、修正旧口令、轮换密钥或清理数据。公开日志前移除凭据、Cookie、分享令牌和用户信息。

## 端口冲突

先确认占用者是本项目的容器、本机开发进程，还是其他项目。Docker 全栈与宿主机开发共用默认 8000/5173，切换方式见 [开发指南](development.md#宿主机开发)。

| 运行方式 | 端口 | 调整位置 |
| --- | --- | --- |
| Docker MySQL/Redis | 宿主机 3306/6380 | 根 `.env` 的 `MYSQL_PORT` / `REDIS_PORT`；宿主机后端需同步 `DB_PORT` / `REDIS_PORT` |
| Docker 后端 | 宿主机 8000 | `docker-compose.yml` 的 `agent-python.ports`，例如 `127.0.0.1:18000:8000` |
| Docker 前端 | 宿主机 5173 | `docker-compose.yml` 的 `frontend-react.ports`，例如 `127.0.0.1:15173:80`；随后访问 `http://localhost:15173` |
| 宿主机后端 | 8000 | 后端 `.env` 的 `PORT`；同步修改 `travel-frontend-react/vite.config.ts` 的代理目标端口 |
| Vite 开发服务器 | 5173 | 用 `npm run dev -- --port 15173 --strictPort` 指定端口；需要跨源访问后端时同步 `AGENT_CORS_ORIGINS` |

Docker 映射只改冒号左侧的宿主机端口；服务间仍通过容器内的 3306/6379/8000/80 通信。
修改 Compose 映射或 env 后，在根目录重新执行 `docker compose --profile apps up -d`，让配置变化生效。
Vite 端口以终端实际输出为准，`--strictPort` 可避免端口占用时悄悄切到其他端口。
