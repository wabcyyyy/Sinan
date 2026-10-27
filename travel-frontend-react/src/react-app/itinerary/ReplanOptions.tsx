import { useState } from 'react'
import { selectReplan } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { ActiveEvidenceNotice } from '../shared/ActiveEvidenceNotice'

export function replanChange(kind: Contracts.ReplanChange['kind'], itemId: number | null): Contracts.ReplanChange {
  return { kind, itemId, dayNo: null, anchorItemId: null, placement: 'before', startTime: null, endTime: null, delayMinutes: null }
}

export function ReplanOptions({ trip, result, onSelected }: {
  trip: ItineraryDetail; result: Contracts.ReplanCompareVO; onSelected: () => void | Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const names = new Map(trip.dayList.flatMap(d => d.items.map(p => [p.id, p.poiName] as const)))
  const choose = async (choice: string) => {
    if (!result.comparisonId) return
    setBusy(true); setError('')
    try {
      await selectReplan(trip.id, { comparisonId: result.comparisonId, planningRevision: result.planningRevision, choice })
      await onSelected()
    } catch (err) { setError(err instanceof Error ? err.message : '选择失败，请重新比较') }
    finally { setBusy(false) }
  }
  return <section className="scenario-content" aria-label="修改方案比较">
    <h3>先选择方案，再核对差异并确认保存</h3>
    <p>当前行程尚未修改。只比较已有安排，固定和已执行项持续保留。</p>
    <ActiveEvidenceNotice report={result.activeEvidence} />
    {result.options.length === 0 && <p role="alert">{result.attempts.some(a => a.status === 'UNKNOWN') ? '时限内未确认可行方案' : '当前要求下未找到可行方案'}，没有覆盖原行程。</p>}
    {result.conflicts.length > 0 && <ul>{result.conflicts.map((c, i) => <li key={i}>{c}</li>)}</ul>}
    <p>{result.attempts.map(a => `${({ local: '受影响区段', day: '当天', trip: '跨天' })[a.scope]}：${a.status === 'INFEASIBLE' ? '当前范围无解' : ['FEASIBLE', 'OPTIMAL'].includes(a.status) ? '有可行候选' : '待确认'}`).join(' → ')}</p>
    <div className="scenario-options">{result.options.map(option => <article key={option.key}>
      <h4>{option.title}</h4>
      <p>受影响：第 {option.affectedDayNos.join('、') || '无'} 天；{option.changedItemIds.map(id => names.get(id) || '安排').join('、') || '现有安排可保留'}</p>
      <dl>
        <dt>改动条目</dt><dd>{option.metrics.changedItems} 项</dd>
        <dt>移除／跨天</dt><dd>{option.metrics.removedItems} 项／{option.metrics.crossDayMoves} 项</dd>
        <dt>相对顺序变化</dt><dd>{option.metrics.orderChanges} 对</dd>
        <dt>开始时间偏移</dt><dd>合计 {option.metrics.startShiftMinutes} 分钟；平均 {option.metrics.meanStartShiftMinutes} 分钟</dd>
        <dt>模型转场时间</dt><dd>{option.metrics.travelMinutes} 分钟</dd>
      </dl>
      <p>{option.numericalStatus === 'OPTIMAL' ? '本次范围内已完成分层求解。' : '已有可行候选，最优性尚未证明。'}{option.factStatus === 'UNKNOWN' && '部分事实仍待核实。'}</p>
      <ul>{option.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul>
      <button type="button" className="button button-primary" disabled={busy || !result.comparisonId} onClick={() => void choose(option.key)}>选此方案，查看差异</button>
    </article>)}</div>
    <p>交通可能含已保存路段或坐标估算，非实时交通。候选不足时只展示实际不同且未被其他候选全面优于的方案。</p>
    {error && <p role="alert">{error}</p>}
  </section>
}
