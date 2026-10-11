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
| 端口冲突 | 调整根 `.env` 的端口；不要停止其他项目的服务 |
| ta-* 容器名冲突 | 已有本项目实例继续使用原实例；隔离验证需独立目录、项目名和容器名 |
| 后端启动拒绝 | JWT/内部令牌可能仍为占位符，检查 env 覆盖顺序 |
| Redis 失败 | 检查口令与是否显式填写错误 REDIS_URL |
| 上传/PDF 不可写 | 容器 UID 1000；人工检查 data 权限后决定调整，不要递归改已有数据 |
| `/demo` 刷新 404 | 静态服务器需要 SPA fallback，项目的 Vite/nginx 已配置 |
| 地图留白 | 需要联网与 WebGL；列表和深链仍可用 |
| 真实生成失败 | 回放不调用模型；检查 provider、模型、key、配额和网络 |

重新初始化不会替换现有 env、修正旧口令、轮换密钥或清理数据。公开日志前移除凭据、Cookie、分享令牌和用户信息。
