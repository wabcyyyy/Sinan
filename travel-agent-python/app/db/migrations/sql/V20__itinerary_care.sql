-- V20：出发前照看任务的持久状态（个人 Agent V1，规格 §4.1「只新增一张持久表」）
--
-- 纪律（同 V1-V19）：一经入库不得再改；后续变更一律新增 V21__*.sql。
-- 执行链：Alembic revision `0020_itinerary_care` 按序执行本文件（不复制、不改写 SQL）。
--
-- 为什么建表：照看任务的「到期扫描时刻 / 最近结果 / 已通知风险键 / 当日 AI 尝试 /
-- 争抢令牌」必须跨进程重启存活——cron.py 的调度登记是内存的，重启即空（规格 §2 行 2
-- 的既有边界）。本表是一趟行程照看状态的唯一权威；每 itinerary_id 一行。
--
-- 权威分工：
--   * 所有者不在此重复存——一律经 itinerary_main.user_id 判定（读行前必复核 owner）；
--   * 用户待确认草稿与聊天消息仍归 itinerary_chat_message / 确认图，本表只存
--     last_result_json 里的 pendingMessageId 引用，不复制草稿内容；
--   * 不把天气检查伪装成生成任务：不写 gen_state，不建 planning_task 行。
--
-- 日期口径：last_ai_attempt_on 是**上海自然日**（Asia/Shanghai，应用侧解释）；
-- 其余时刻由应用侧时钟写入，与 V16/V17 同源（部署保持应用与库同时区）。

CREATE TABLE itinerary_care (
  itinerary_id          BIGINT      NOT NULL COMMENT '行程ID：任务身份，一行=一趟行程；所有者从 itinerary_main 读取，不重复保存 user_id',
  enabled               TINYINT(1)  NOT NULL DEFAULT 0 COMMENT '用户授权开关：1=照看已开启，同时表示同意按现有额度机制消耗后台 AI；0=暂停（停止未来检查，已呈现草稿保留）',
  watched_item_ids_json JSON        NULL COMMENT '用户关注天气影响的活动 item ID（1-20 个当前行程 attraction）；行程编辑后已删除的 ID 不再监控，不按同名重绑',
  next_check_at         DATETIME    NULL COMMENT '到期扫描时刻：开启时置为当前时间；3 日窗口外保留任务等待进入窗口；NULL=不参与扫描',
  last_checked_at       DATETIME    NULL COMMENT '最近一次检查完成时刻（checkedAt：查询时间，可能命中天气缓存，不冒充上游更新时间）',
  last_status           VARCHAR(24) NULL COMMENT '用户可见状态：pending|no_risk|unavailable|waiting_for_user|failed|disabled 等（简短可理解，不写成功以外的虚构）',
  last_result_json      JSON        NULL COMMENT '有界结果：逐日观测、受影响项、简短原因、pendingMessageId；不复制整份 plans',
  notified_risks_json   JSON        NULL COMMENT '已呈现或已忽略的风险键（行程+受影响日期+风险类别）：去重权威，只保留本行程日期范围内的键',
  last_ai_attempt_on    DATE        NULL COMMENT '上海自然日：每趟行程每个上海自然日最多一次自动 AI 尝试，外呼前原子写入；失败/超时/崩溃不在当日自动重烧',
  check_token           VARCHAR(36) NULL COMMENT '当前持有者令牌：立即检查与 cron 的条件 UPDATE 争抢；失效 token 不得发布结果',
  checking_until        DATETIME    NULL COMMENT '争抢截止时刻：过期 token 可重领（进程崩溃后无需恢复中途推理）',
  created_at            DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (itinerary_id),
  KEY idx_itinerary_care_scan (enabled, next_check_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='行程照看：出发前天气照看任务的持久状态（V20）';
