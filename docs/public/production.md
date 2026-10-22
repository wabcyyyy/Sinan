# 小规模试用部署（Railway）

当前交付提供部署镜像、邀请码、一次性 6 元赠额与最小后台。公网部署、目标网络体验及真实模型质量须在你的 Railway 账号完成验收；本地测试和固定回放不代表这些验收已通过。

当前真实后端暂缓部署。完整本地拓扑已拆为 API 与独立规划 worker，见 [执行说明](planning-worker.md)。下文 Railway 单服务拓扑是历史配置参考，不能单独运行完整规划；跨主机检查点和文件存储尚未迁移，不能据此直接宣称已具备托管高可用。

## 历史单实例部署参考

首版使用 Railway 的一个项目：Sinan 应用服务 + MySQL + Redis。前端构建产物由同一 FastAPI 进程交付，使用同源 Cookie；Railway 提供默认域名和自动 HTTPS，不需要先购买域名、服务器或配置 Caddy。保持一个应用实例、关闭自动休眠，不开多副本或发布重叠；持久卷版本切换允许短暂停机。

1. 创建项目，添加 MySQL、Redis；二者只使用私网连接，不创建公开 TCP 代理。
2. 从 GitHub 添加应用服务，工作目录为仓库根目录，Dockerfile 选择 `Dockerfile.hosted`。不要选择 Vercel Functions：当前实现依赖常驻后台池、进程内事件总线和持久目录。
3. 给应用挂载持久卷 `/app/data`。镜像入口只修正卷根目录权限，然后降为 UID/GID 1000；检查点、用量历史、上传与 PDF 均在该卷。不要在 pre-deploy 阶段操作卷；迁移在应用主入口执行。
4. 设置 [变量模板](../../deploy/railway.env.example) 中的值。MySQL 管理连接创建 `sinan` 用户，只授予业务数据库权限（含首启迁移所需 DDL），再将其口令填入应用变量。不要把 root 账号放进应用。
5. 设置端口 8000、健康检查路径 `/healthz`；模板已包含启动等待 300 秒、停止宽限 45 秒（`RAILWAY_DEPLOYMENT_DRAINING_SECONDS`）及 overlap 0 秒。Railway 默认停止宽限为 0，不能遗漏此项。应用池共享 30 秒 drain 预算，未完成任务由已有恢复逻辑接管。设置应用与 MySQL 为 UTC。
6. Generate Domain，打开 `/demo`、`/privacy`。镜像默认启用 Secure Cookie、邀请码、用户生成并发限制和计费；模型 key 未填或单价未配置时真实 AI 不可用。

模型单价配置格式为 `{"完整base_url|模型名":{"input":非缓存输入单价,"cached_input":缓存输入单价,"output":输出单价}}`，单位元/百万 token。必须按实际网关价格填写；不提供推测的默认价格。应用按返回 usage 估算消耗，不是供应商账单；reasoning token 应包含在供应商的 completion_tokens 中。暂不开放另行收费的联网模型插件和语音识别，避免绕过计费。搜索开关在托管镜像默认关闭，缺证据仍展示估算/待核实。

为试用创建独立 API key，配置供应商可用的支出上限、关闭自动充值。每用户 6 元不包含服务器费用，也不是供应商扣款的绝对保证：调用异常、供应商额外收费及价格变更仍需对账。20 位新用户的初始赠额合计 120 元，仅为应用预算。

## 管理与额度

在应用服务终端执行 `python scripts/trial_admin.py invites --count 1`，用此码注册自己的账号，再执行 `python scripts/trial_admin.py promote 你的用户名`。重新登录后访问 `/admin`。后台提供：发邀请码、分页用户列表、禁用/启用、余额和最近 100 条费用、手动赠额。邀请码明码仅发放时返回，不入库、不进日志。

注册赠额和邀请码核销同一事务完成；邀请码只能成功核销一次。注册赠送 6 元，之后不会按月或登录重置。澄清、生成、编辑共享每天 20 次操作的速率保护；未交付天补齐仍沿用原配额豁免，真实模型调用照常消耗余额。

每次调用先按文本字节上界与最大输出保守冻结，然后按服务端配置单价和供应商 usage 结算；并发调用原子占用余额。重试也是新的真实请求。缺失 usage、超时、取消或进程中断保留冻结额，重启后转 unknown，不自动退款。管理员核查供应商记录后，在费用表填写金额和原因对账；在途 reserved 不能人工结算。配置或上游超出预留时如实记录实际消耗，余额可显示负值并阻止后续调用，不能静默截断账单。自带网关不扣平台赠额；独立付费外部报价不在赠额账本内，首版不要配置其 key。

发邀请码时同时提供真实联系渠道、数据删除流程和备份保留期限；上线前替换隐私页的泛称联系说明。禁用账号立即阻止后续鉴权请求；已发出的模型请求不能撤回。

## 运维、备份与回退

`/healthz` 检查 MySQL/Redis，失败返回 503，不会调用模型。每分钟依赖和生成失败检查复用 `LLM_ALERT_WEBHOOK_URL`，没有配置时仅记录日志；应连接你能收到的通知服务，实测失败及恢复各一次。健康检查用于发布准入，仍需平台监控持续拨测。管理员后台既有 `/api/admin/llm-usage` 和 `/api/admin/agent-metrics` 可用于诊断。

在 Railway 对 MySQL 卷和应用卷启用每日备份并检查成功通知。平台分别备份两个卷不等于跨卷一致快照：需要完整恢复点时，安排短维护窗口，停止应用及所有写入者后分别备份 MySQL 与应用卷，记录这对备份的标识，再启动同一应用版本。环境密钥单独备份，Redis 吊销记录/会话状态也须保留；恢复旧认证状态前轮换 JWT，使旧会话失效。保存独立于运行项目的加密异机副本，避免项目误删同时丢失运行数据和全部备份。

现有 `scripts/backup_restore.py` 是 **Compose 专用的停写备份工具**，不能直接对 Railway 执行；它覆盖 MySQL 和整个 data，不包含 Redis、环境密钥或 MySQL 账号。平台计划任务、通知接收和异机存储须在账号中实际配置，仓库不会假装已经配置。

首次开放试用前，在独立项目恢复这对 MySQL/应用卷备份，保留原环境不动，检查迁移版本、用户/余额/费用流水、行程、图片/PDF。恢复失败保留失败现场，绝不覆盖原库。固定发布提交及镜像，保留上一版本；应用回退先确认数据库向后兼容，迁移均只追加建表（当前至 V20），不提供 downgrade，不将“回退代码”当成数据库回退。单次赋额和对账信息必须随数据库一起恢复。

## 验收边界

- 本地可执行：`docker compose -p sinan-hosted-test -f compose.hosted-test.yml up -d --build --wait --wait-timeout 300`。之后在后端目录执行 `uv run python scripts/verify_hosted.py`，验证真实 MySQL 注册/余额/权限、停写备份、独立空库及数据目录恢复后再登录。全程不调用模型。这是隔离验收，端口仅回环 18000，示例口令禁止用于公网；验证后 `docker compose -p sinan-hosted-test -f compose.hosted-test.yml stop`，保留卷。
- 目标域名必须验证 HTTPS 登录、Secure/HttpOnly Cookie、普通用户管理 API 403、Agent/MCP/docs 不可达、刷新/断线恢复、卷持久化、停止与恢复、回退。
- `RAILWAY_EDGE_HEADERS=true` 使用平台提供的 X-Real-IP，忽略自带 XFF。只用于唯一公网入口是 Railway HTTP edge 的部署。必须以两个实际来源 IP 和伪造 XFF/X-Real-IP 验证平台覆写语义；应用单测不能代替平台验收。未验证前不要宣称防伪造已通过。
- 真实模型验收另定总预算，覆盖普通多日、到离窗口、必去/排除、严格预算、编辑确认及中断恢复，保留全部失败和 unknown；固定回放不计入真实成功率。不能为了“验收绿”放松门禁或反复重跑覆盖失败。

平台事实以官方文档为准：[域名与 HTTPS](https://docs.railway.com/networking/public-networking)、[持久卷](https://docs.railway.com/volumes)、[停止宽限](https://docs.railway.com/deployments/deployment-teardown)、[边缘限制与请求头](https://docs.railway.com/networking/public-networking/specs-and-limits)。
