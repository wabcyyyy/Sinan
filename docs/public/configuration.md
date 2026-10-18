# 配置

初始化从根目录 `.env.compose.example` 与后端 `.env.example` 创建本地配置，生成随机口令与令牌。现有文件逐字节保留；缺失文件会创建，已有错误值不会自动修复。模型 key 默认留空。

## 生效顺序

- Compose 后端：显式 `environment` > 根 `.env` 的 env_file > 后端 `.env` 的 env_file。
- MySQL、Redis 容器地址与口令由 Compose 显式环境统一注入。
- 宿主机开发：进程环境变量 > 根 `.env` > 后端 `.env`。

根 `.env` 中已有同名应用键会覆盖后端文件，修改前确认生效来源。

| 用途 | 位置 | 说明 |
| --- | --- | --- |
| MySQL/Redis 口令与端口 | 根 `.env` | 初始化生成强口令；默认端口 3306/6380 |
| JWT 与内部令牌 | 后端 `.env` | 初始化生成；根文件同名键优先 |
| 模型网关、模型名与 key | 后端 `.env` | `/demo` 不需要，真实生成需要 |
| 外部服务与可选功能 | 后端 `.env.example` | 完整字段与注释以此为准 |

真实生成填写 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` 后重新创建后端容器。联网搜索和具名 provider 以完整模板说明为准。

完整 Docker 栈在仓库根目录执行：

```bash
docker compose --profile apps up -d --force-recreate agent-python
```

宿主机开发则停止并重新运行后端进程。已有 env 不会被初始化器修复或覆盖，修改时先确认上面的生效顺序。
不要把示例占位符当作凭据；不要为 Compose 显式设置指向 localhost 的 `REDIS_URL`，它优先于派生地址。
端口冲突时调整根文件的 MYSQL_PORT/REDIS_PORT，不要停止其他项目的服务。

环境文件、用户数据、Cookie 与密钥不得提交。使用 `docker compose config --quiet` 检查语法，不要把完整配置贴到公开日志。
