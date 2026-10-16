-- V17：累计成本预留与结算的持久费用账本（上线 v1 T2，PLAN §5「建立累计成本预留与结算机制」）
--
-- 纪律（同 V1-V16）：一经入库不得再改；后续变更一律新增 V18__*.sql（V18 已分配给 T7）。
-- 执行链：Alembic revision `0017_cost_ledger` 按序执行本文件（不复制、不改写 SQL）。
--
-- 为什么需要这两张表（现状缺口，逐条对 PLAN §5 T2）：
--   ① 预留只在内存里。`RunLimits._reservations` 挂在进程内的 dataclass 上，`persist` 落的是
--      **计数器快照**，不是「哪一笔请求还没结算」。进程一死，未结算的预留既不知道是谁的、
--      也无法判定它是否已被结算过——幂等与「崩溃后有界」都无从谈起。
--   ② 硬费用顶在 Redis。`quota_service` 的分钟/日窗走 `state_and_sessions.sliding_hit`，
--      Redis 熔断期退回进程内表、恢复后又从空计数重新开始（该文件 `_bound_windows` 的注释
--      自己写明了这个方向）。**费用账本被可丢的存储承载 = 一次 Redis 故障即归零**，
--      PLAN §6「任意失败重试与账号数量都绕不过平台硬费用上限」直接失守。
--      所以本迁移把硬顶搬进 MySQL 业务面；Redis 只继续承担**速率**（每分钟几次点击），
--      不再承担**钱**。
--   ③ 补齐额度没有行程级上限。失败天补齐刻意免扣用户「每日生成次数」（P0-3 拍板），
--      但当时只留了分钟窗 + 「已成功天不能再补」两条闸；跨行程反复失败重试拿不到新的
--      **运行**预算（V16 的 consumed_* 已封），却也没有任何**补齐次数**与**退避**记录。
--
-- 权威分工（一个事实一个权威存储，别处只读不复制）：
--   cost_ledger        = 准入用的**费用/额度账本**（用户日窗、平台日总额、行程补齐额度与退避、
--                        平台熔断状态）。这是「还能不能再发一个请求」的唯一判据。
--   llm_reservation    = **单笔请求的生命周期**（OPEN→SETTLED/ABANDONED）与归属
--                        （platform / byok）。这是「预留只结算一次、崩溃未结算的请求有界」的
--                        唯一权威。
--   planning_task.consumed_*（V16） = 交付责任的**运行消耗计量**（跨 attempt 继承的执行上限基线）。
--                        费用硬顶绑在它之上，本迁移不往那张表里加费用列，也不在此重复 token 账。
--   llm_calls（usage_store，SQLite 内部库） = **调用明细**（每次 HTTP 尝试一行）：仪表盘、账单
--                        核对、排障归因。**不是**准入依据，也不承担幂等结算。
--
-- 单位口径：**只有 token 与调用次数，没有任何人民币金额**。本仓没有可信定价表
-- （模型价随供应商/汇率变），编造 CNY 会造出一份看着精确、实则无人能负责的账。
-- 没有可信定价时用 token 硬顶，是 PLAN §5 T2 的原话。
--
-- 时刻由应用侧时钟写入（与 V16/planning_task 同源，单进程 v1 只有一个钟）。

CREATE TABLE cost_ledger (
  scope_key                VARCHAR(96)  NOT NULL COMMENT '账本键：platform:YYYYMMDD | user:{uid}:YYYYMMDD | trip:{itinerary_id}（唯一权威入口，见 cost_policy.scope_key）',
  scope_kind               VARCHAR(16)  NOT NULL COMMENT 'platform|user|trip：窗口归属哪一层（用途互不重叠，见 cost_policy.CEILINGS）',
  window_day               INT          NOT NULL DEFAULT 0 COMMENT '日窗 = YYYYMMDD；0 = 终身窗（trip 补齐额度用，随行程存续）',
  attribution              VARCHAR(16)  NOT NULL DEFAULT 'platform' COMMENT 'platform=烧运营方 key；byok=用户自带网关；any=trip 行（补齐额度与归属无关）。用户日窗按归属分行，平台行只收 platform 腿',
  charge_prompt_tokens     BIGINT       NOT NULL DEFAULT 0 COMMENT '已计入的 prompt token（预留时按保守值入账，结算时真值调整；usage 未知/ABANDONED 不回吐）',
  charge_completion_tokens BIGINT       NOT NULL DEFAULT 0 COMMENT '已计入的 completion token（同上）',
  charged_calls            INT          NOT NULL DEFAULT 0 COMMENT '已计入的模型调用次数（含失败尝试：失败一样花钱）',
  open_reservations        INT          NOT NULL DEFAULT 0 COMMENT '当前 OPEN 的预留笔数：超上限即拒绝再预留（崩溃循环的有界性靠这一列，不靠运气）',
  abandoned_reservations   INT          NOT NULL DEFAULT 0 COMMENT '因进程崩溃未结算而被收口的笔数（额度保留、不可再结算）',
  settled_count            INT          NOT NULL DEFAULT 0 COMMENT '已结算笔数（与 llm_reservation.state=SETTLED 同口径，供对账；明细权威仍是该表）',
  repairs                  INT          NOT NULL DEFAULT 0 COMMENT 'trip 专用：本行程已使用的补齐额度（跨 attempt 累计，免扣的是用户日次数、不是补齐额度）',
  repair_failures          INT          NOT NULL DEFAULT 0 COMMENT 'trip 专用：连续补齐失败次数（成功即清零），失败退避的指数底数',
  backoff_until            DATETIME     NULL COMMENT 'trip 专用：退避到期时刻；未到即拒绝新的补齐请求（挡「反复失败重试刷额度」）',
  breaker_tripped_at       DATETIME     NULL COMMENT '非空 = 该窗口已触顶并熔断：停止发放新请求，直到窗口滚动（日窗次日自然换新行）',
  created_at               DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at               DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (scope_key),
  KEY idx_cost_ledger_kind_day (scope_kind, window_day),
  KEY idx_cost_ledger_trip (window_day, repairs)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='成本账本：用户/平台/行程硬顶的持久权威（V17）';

CREATE TABLE llm_reservation (
  reservation_id            VARCHAR(36)  NOT NULL COMMENT '预留令牌（RunLimits.reserve_llm 返回的 uuid）：结算以它为主键做 CAS，天然幂等',
  scope_key                 VARCHAR(96)  NOT NULL COMMENT '主归属账本键（有行程时记 trip，否则记该归属下的 user 日窗）；仅用于按行程/用户反查',
  charged_scopes            VARCHAR(255) NOT NULL COMMENT '预留实际级联记账的窗口键清单（逗号分隔，顺序即取锁顺序）。结算与收口必须回放到预留时那一批行：跨日结算时"当前日窗"已换行，按 now() 重算会把差值调到没被预留过的行上。账本总额仍是权威，本列只是结算路由',
  attribution               VARCHAR(16)  NOT NULL COMMENT 'platform|byok：这一笔真实烧的是谁的 key（计费归属的唯一持久记录）',
  attribution_note          VARCHAR(48)  NULL COMMENT '归属降级原因，目前只有 byok_lookup_failed（用户网关查不到而回落到运营方 key）：不得静默，运营据此追责',
  task_id                   VARCHAR(32)  NULL COMMENT '规划任务令牌（无任务的入口为空）',
  user_id                   BIGINT       NULL COMMENT '归属用户',
  itinerary_id              BIGINT       NULL COMMENT '归属行程',
  scene                     VARCHAR(32)  NOT NULL DEFAULT 'other' COMMENT '业务场景（generate/research/chat/assist/...），与 usage_store 的 scene 同口径',
  reserved_prompt_tokens     BIGINT      NOT NULL DEFAULT 0 COMMENT '预留的 prompt token（按请求体字节数保守估算）',
  reserved_completion_tokens BIGINT      NOT NULL DEFAULT 0 COMMENT '预留的 completion token（= 请求的 max_tokens）',
  charged_prompt_tokens      BIGINT      NULL COMMENT '结算后的 prompt token；usage 未知时等于预留值（保守：宁可多算，绝不少算）',
  charged_completion_tokens  BIGINT      NULL COMMENT '结算后的 completion token；同上',
  usage_complete            TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '供应商是否回了完整 usage：0 = 结算值含保守估计，不得当作真实账单',
  state                     VARCHAR(16)  NOT NULL COMMENT 'OPEN|SETTLED|ABANDONED：SETTLED/ABANDONED 都是终态，只有 OPEN 可被结算，因此一笔预留恰好结算一次',
  created_at                DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '预留时刻（发起 HTTP 请求之前，持久预留先于外呼）',
  expires_at                DATETIME     NOT NULL COMMENT '结算宽限到期时刻：过期仍 OPEN = 持有它的进程已死，由清理任务收口为 ABANDONED（额度保留）',
  settled_at                DATETIME     NULL COMMENT '结算/收口时刻',
  PRIMARY KEY (reservation_id),
  KEY idx_llm_reservation_state_expires (state, expires_at),
  KEY idx_llm_reservation_scope (scope_key),
  KEY idx_llm_reservation_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模型调用预留：持久预留/幂等结算/崩溃收口的权威（V17）';
