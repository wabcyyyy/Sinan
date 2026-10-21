import type { ReactNode } from 'react'

import { evidenceLabel, evidenceTone, type EvidenceShape } from '../../shared/evidence'
import type { ItineraryDetail, TripItem } from '../../types/itinerary'
import { CountUp } from '../shared/motion'

/**
 * React 详情页的可测展示片段（从 TripDetailPage 抽出）。
 *
 * 抽出的唯一理由：React 树此前零测试，而这些文案/徽章正是审查 P1-2/P1-4 点名的
 * 缺口（草案横幅、预算行的估算字样、项级徽章）——用纯函数式展示组件承载，
 * 测试可直接渲染断言，不需要起网络/路由。
 */

/** 预算行（P1-4）：金额是 AI 估算，口径必须写出来，不能只叫"预算参考"。 */
export function budgetCaption(): string {
  return '预算参考 · 估算'
}

/** 草案横幅文案（P1-2）：destinationStatus=draft_only 时的面级提示。 */
export const DRAFT_ONLY_TEXT = '本次为未核实草案，点位未经数据源验证'

/** 草案提示落在 hero 标题下、替代同位的 QualityNotice（两者都在说「草案、出发前核实」，
 * 不再页顶一条、标题下再一条）；待核实条数并进同一块。 */
export function DraftOnlyBanner({ status, pending = 0 }: { status?: string | null; pending?: number }): ReactNode {
  if (status !== 'draft_only') return null
  return (
    <div className="detail-draft-banner" role="status">
      <span>未核实草案</span>{pending > 0 && <small>{pending} 项待核实</small>}<p>{DRAFT_ONLY_TEXT}。价格与营业时间均为估算，出发前请逐项核实。</p>
    </div>
  )
}

/** 项级徽章：唯一映射来自共享模块（P1-4）。 */
export function itemEvidenceText(item: EvidenceShape & Partial<TripItem>): string {
  return evidenceLabel(item) || '已核实'
}

export function ItemEvidence({ item }: { item: TripItem }): ReactNode {
  return <span className={evidenceTone(item) === 'warning' ? 'fact-warning' : ''}>{itemEvidenceText(item)}</span>
}

export function DietaryEvidenceNotice({ trip }: { trip: ItineraryDetail }): ReactNode {
  const issues = [...(trip.qualityReport?.warnings || []), ...(trip.qualityReport?.blockingIssues || [])]
    .filter(issue => issue.code === 'DIETARY_EVIDENCE_REVIEW')
  if (!issues.length) return null
  return <div className="detail-draft-banner" role="status" aria-label="饮食要求核实">
    <span>{trip.qualityStatus === 'BLOCKED' ? '行程未完成，请核实餐饮与饮食要求' : '餐饮与饮食要求'}</span>
    {issues.map((issue, index) => <p key={index}>{issue.message}</p>)}
  </div>
}

/** 详情页指标行（预算/日期/天数）——预算行的估算口径在此单点定义。
 * 没有出发日期时不单占一张「待定」卡，并进天数卡的说明行。 */
export function TripMetrics({
  trip,
  dateText,
  weatherDays,
  hasWeather,
}: {
  trip: ItineraryDetail
  dateText: string | null
  weatherDays: number
  hasWeather: boolean
}): ReactNode {
  const budgetWarning = [...(trip.qualityReport?.warnings || []), ...(trip.qualityReport?.blockingIssues || [])]
    .find(issue => issue.code === 'BUDGET_EXCEEDED')
  const items = (trip.dayList || []).flatMap(day => day.items)
  const hasCost = trip.totalAmount > 0 || (trip.budgetList || []).some(row => row.amount > 0) || items.some(item => item.cost != null)
  const missingCost = items.some(item => item.cost == null)
  return (
    <div className="detail-metrics">
      <span>
        <strong>{hasCost ? <CountUp value={trip.totalAmount ?? 0} prefix="￥" /> : '待估算'}</strong>
        <small>{hasCost ? (missingCost ? '部分费用估算 · 尚有条目未计价' : budgetCaption()) : '费用尚未填写'}</small>
        {budgetWarning && <small className="fact-warning" role="status">{budgetWarning.message}</small>}
      </span>
      {dateText && <span>
        <strong>{dateText}</strong>
        <small>出发日期</small>
      </span>}
      <span>
        <strong>{weatherDays} 天</strong>
        <small>{`${dateText ? '' : '日期未定 · '}${hasWeather ? '天气已接通' : '路线草案'}`}</small>
      </span>
    </div>
  )
}
