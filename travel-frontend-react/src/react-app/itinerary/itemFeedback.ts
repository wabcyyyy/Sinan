/**
 * 条目对/错反馈（C3.5）的纯逻辑：口径门控、回显索引、原因中文映射。
 *
 * 后端口径（app/schemas/feedback.py，单一真源）：值域二元 right/wrong；
 * wrong 必带 reason、right 恒不带；note 可选 ≤200 字（后端 400 兜底）。
 * 视图层只做镜像与提前拦截，改口径先改 schema 再重新导出契约。
 */
import type * as Contracts from '../../types/generated/contracts'

export type FeedbackReason = NonNullable<Contracts.FeedbackCreate['reason']>
export type FeedbackValue = Contracts.FeedbackCreate['value']

/** 英文入库、中文展示（契约注释口径）；顺序即选择器展示顺序。 */
export const FEEDBACK_REASON_LABELS: Readonly<Record<FeedbackReason, string>> = {
  wrong_location: '位置不对',
  wrong_time: '时间不对',
  wrong_price: '价格不对',
  not_interested: '不感兴趣',
  closed: '已关门/停业',
  other: '其他',
}

export const FEEDBACK_REASON_ORDER: readonly FeedbackReason[] = Object.keys(FEEDBACK_REASON_LABELS) as FeedbackReason[]

/**
 * 组装提交体；口径不满足（wrong 未选原因）返回 null，由调用方提示而不是发请求。
 * 契约 FeedbackCreate 的 reason/note 是 required-nullable（schema required 含两者、
 * 类型可 null）：显式 null 与后端默认 None 等价；right 的 reason 恒为 null——
 * 后端 model_validator 会拒绝 right 携带非空 reason。
 */
export function buildFeedbackPayload(itemId: number, value: 'right', reason?: FeedbackReason, note?: string): Contracts.FeedbackCreate
export function buildFeedbackPayload(
  itemId: number,
  value: 'wrong',
  reason?: FeedbackReason,
  note?: string,
): Contracts.FeedbackCreate | null
export function buildFeedbackPayload(
  itemId: number,
  value: FeedbackValue,
  reason?: FeedbackReason,
  note?: string,
): Contracts.FeedbackCreate | null {
  if (value === 'right') return { itemId, value, reason: null, note: null }
  if (!reason) return null
  const trimmed = note?.trim()
  return { itemId, value, reason, note: trimmed || null }
}

/** GET 回显列表 → itemId 索引；一人一条（upsert）所以无同键冲突。 */
export function feedbackIndex(feedbacks: Contracts.FeedbackVO[] | undefined | null): Map<number, Contracts.FeedbackVO> {
  return new Map((feedbacks ?? []).map((item) => [item.itemId, item]))
}
