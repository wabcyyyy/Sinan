-- V16：规划任务的持久化执行面（上线 v1 T1，PLAN §5「建立持久化规划任务执行机制」）
--
-- 纪律（同 V1-V15）：一经入库不得再改；后续变更一律新增 V17__*.sql。
-- 执行链：Alembic revision `0016_planning_task` 按序执行本文件（不复制、不改写 SQL）。
--
-- 为什么建表：执行所有权此前只有一份进程内活跃表（app/services/planning_tasks.py 的 _tasks），
-- 任务预算另存在 checkpoint SQLite 的 task_budgets 表里。两个结构性后果：
--   ① 进程重启后活跃表为空，恢复扫描只能凭"预算还剩多少"裁决，而那份预算的剩余时间是从
--      提交时刻的墙钟反推的（task_budget.load 用 submitted_at 重建 started_at）——重启越晚
--      越容易判成过期，于是"默认恢复时预算已过期"，本该续跑的任务被终态化；
--   ② 旧 worker 不知道自己已经失权（活跃表是唯一依据），继续往同一行程写天、写终态，
--      与新 worker 双写。
-- 本表把「任务状态 + 绝对截止 + 租约/心跳 + attempt + 累计消耗」统一到业务数据面：
--   * 所有权由 (task_id, lease_owner) 的条件更新判定，失权写入 rowcount=0 直接抛错，
--     不再依赖进程内表；
--   * 预算剩余 = deadline_at - now（绝对时刻），与进程存活时长无关；
--   * 累计消耗随 attempt 继承：用户显式补齐或恢复重拉只推进 attempt，不清零计数器，
--     所以反复重试拿不到新的调用/token 额度（运营额外放行的修复预算归 T2）。
-- checkpoint SQLite 只保留 LangGraph 检查点职责：task_budgets 表随本迁移退役，代码不再
-- 读写它（存量行不搬、不删：它记录的是已过期任务的旧预算，没有产品价值，见 README 现状说明）。
--
-- 时刻一律由应用侧时钟写入（与 generation_recovery 的僵尸判定同源，部署保持应用与库同时区）：
-- 单进程 v1 只有一个时钟，用 DB 时钟反而让"谁在心跳"变成两处口径。

CREATE TABLE planning_task (
  itinerary_id        BIGINT       NOT NULL COMMENT '行程ID：一趟行程同一时刻只有一个任务行（交付责任随 attempt 延续）',
  user_id             BIGINT       NOT NULL COMMENT '提交用户ID：排队准入按人计数（重启后仍成立）',
  task_id             VARCHAR(32)  NOT NULL COMMENT '当前持有者令牌：每次 acquire 轮换，旧 worker 的写入据此被拒',
  lease_owner         VARCHAR(64)  NOT NULL COMMENT '持有 worker 身份（pid+进程随机段）：进程重启即换新身份',
  attempt             INT          NOT NULL DEFAULT 1 COMMENT '交付责任内的尝试序号：首次提交=1；接管/显式补齐 +1，累计消耗不清零',
  state               VARCHAR(16)  NOT NULL COMMENT 'QUEUED|RUNNING|FINISHED|FAILED|CANCELLED|EXPIRED（执行态，产品态由 itinerary_main 投影）',
  submit_at           DATETIME     NOT NULL COMMENT '首次提交时刻（交付责任起点，不随 attempt 变）',
  deadline_at         DATETIME     NOT NULL COMMENT '当前 attempt 的绝对截止时刻：预算剩余=本列-now；接管沿用原值，显式补齐重开窗口',
  lease_expires_at    DATETIME     NULL     COMMENT '租约到期时刻：心跳/每次预算写入续期；过期即视为 worker 已死、可被恢复接管',
  heartbeat_at        DATETIME     NULL     COMMENT '最近一次心跳/预算写入时刻（接管与运维观测用）',
  cancel_requested_at DATETIME     NULL     COMMENT '用户取消请求时刻：非空即停止后续外呼与写入',
  consumed_llm_calls  INT          NOT NULL DEFAULT 0 COMMENT '本责任累计模型调用次数（跨 attempt 只增，重试拿不到新额度）',
  consumed_prompt_tokens     BIGINT NOT NULL DEFAULT 0 COMMENT '累计 prompt token（结算口径，供 T2 的费用硬顶绑定）',
  consumed_completion_tokens BIGINT NOT NULL DEFAULT 0 COMMENT '累计 completion token（同上）',
  consumed_retrievals        INT    NOT NULL DEFAULT 0 COMMENT '累计检索/证据类调用次数',
  consumed_research_calls    INT    NOT NULL DEFAULT 0 COMMENT '累计研究段调用次数',
  consumed_existence_checks  INT    NOT NULL DEFAULT 0 COMMENT '累计存在性解析次数',
  consumed_replans           INT    NOT NULL DEFAULT 0 COMMENT '累计局部重规划次数',
  budget_json         LONGTEXT     NOT NULL COMMENT '当前 attempt 的 RunLimits 快照（计数器+上限），形状由 app/agent/runtime/run_limits.py 定义',
  result_code         VARCHAR(32)  NULL     COMMENT '终态原因码：稳定错误码或 CANCELLED/EXPIRED/OWNERSHIP_LOST',
  created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (itinerary_id),
  KEY idx_planning_task_user_state (user_id, state),
  KEY idx_planning_task_deadline (deadline_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='规划任务：状态/租约/预算的单一权威（V16）';
