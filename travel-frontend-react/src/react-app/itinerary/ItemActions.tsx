import { useState } from 'react'
import { actOnItem, compareReplanning } from '../../api/sinan'
import type { ReplanCompareVO } from '../../types/generated/contracts'
import { ReplanOptions, replanChange } from './ReplanOptions'
import type { DayPlan, ItineraryDetail, TripItem } from '../../types/itinerary'
import type { ItemActionBody, ItemCreateBody } from '../../types/generated/contracts'
import { UserArrangementForm, type UserArrangement } from '../shared/UserArrangementForm'
import './scenario.css'

export function ItemActions({ trip, day, item, disabled, onChanged, onDraftCreated }: {
  trip: ItineraryDetail; day: DayPlan; item: TripItem; disabled: boolean; onChanged: (trip: ItineraryDetail) => void; onDraftCreated?: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [replan, setReplan] = useState<ReplanCompareVO | null>(null)
  const [deleting, setDeleting] = useState(false)
  const fixed = trip.fixedArrangements?.some(f => f.dayNo === day.dayNo && f.name === item.poiName && f.itemType === item.itemType)
  const locked = disabled || busy || item.id === undefined || trip.planningRevision === undefined
  const costCurrency = day.constraintPlanning && item.cost != null ? item.costCurrency || '币种待确认' : trip.budgetCurrency || 'CNY'
  const costReadOnly = Boolean(day.constraintPlanning && item.cost != null && (!item.costCurrency || item.costCurrency !== trip.budgetCurrency))
  const initial: UserArrangement = { dayNo: day.dayNo, itemType: item.itemType as ItemCreateBody['itemType'], poiName: item.poiName,
    startTime: item.startTime?.slice(0, 5) || '', endTime: item.endTime?.slice(0, 5) || '', address: item.address ?? null, cost: item.cost ?? null, remark: item.remark ?? null, fixed: Boolean(fixed) }
  const run = async (action: ItemActionBody['action'], value = initial, targetIndex?: number) => {
    setBusy(true); setError('')
    try {
      const temporalEdit = action === 'update' && (value.dayNo !== day.dayNo || value.startTime !== item.startTime?.slice(0, 5) || value.endTime !== item.endTime?.slice(0, 5))
      if (day.constraintPlanning && (action === 'move' || action === 'delete' || temporalEdit)) {
        if (temporalEdit && (value.poiName.trim() !== item.poiName || (value.address || null) !== (item.address || null)
          || (value.cost == null ? null : Number(value.cost)) !== (item.cost == null ? null : Number(item.cost)) || (value.remark || null) !== (item.remark || null))) {
          throw new Error('请先单独保存地点、费用或备注，再比较时间调整；本次尚未保存。')
        }
        const change = replanChange(action === 'delete' ? 'close' : 'move', item.id!)
        if (action !== 'delete') change.dayNo = value.dayNo
        if (temporalEdit) { change.startTime = value.startTime; change.endTime = value.endTime }
        if (action === 'move' && value.dayNo === day.dayNo && targetIndex !== undefined) {
          change.anchorItemId = day.items[targetIndex]?.id ?? null
          change.placement = targetIndex < day.items.indexOf(item) ? 'before' : 'after'
        }
        setReplan(await compareReplanning(trip.id, { planningRevision: trip.planningRevision!, change,
          completedItemIds: trip.dayList.flatMap(d => d.executedItemIds || []), optionalItemIds: [], allowCrossDay: true }))
        setEditing(false); setDeleting(false)
        return
      }
      const next = await actOnItem(trip.id, item.id!, { action, planningRevision: trip.planningRevision!, dayNo: value.dayNo,
        startTime: value.startTime || null, endTime: value.endTime || null, poiName: value.poiName,
        address: value.address, cost: value.cost, remark: value.remark, targetIndex: targetIndex ?? null })
      onChanged(next); setEditing(false); setDeleting(false)
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败，请重试') }
    finally { setBusy(false) }
  }
  return <div className="item-actions" onClick={e => e.stopPropagation()}>
    <div className="item-action-buttons">
      <button type="button" disabled={locked} onClick={() => void run(fixed ? 'unfix' : 'fix')}>{fixed ? '已固定 · 解除' : '固定安排'}</button>
      <button type="button" disabled={locked || fixed} aria-expanded={editing} onClick={() => { setEditing(!editing); setDeleting(false); setError('') }}>编辑安排</button>
      <button type="button" disabled={locked || fixed} onClick={() => { setDeleting(true); setEditing(false); setError('') }}>移除</button>
      <button type="button" disabled={locked || fixed || day.items.indexOf(item) === 0} onClick={() => void run('move', initial, day.items.indexOf(item) - 1)}>↑ 上移</button>
      <button type="button" disabled={locked || fixed || day.items.indexOf(item) === day.items.length - 1} onClick={() => void run('move', initial, day.items.indexOf(item) + 1)}>↓ 下移</button>
      <label>移到 <select aria-label={`移动${item.poiName}到其他天`} value={day.dayNo} disabled={locked || fixed} onChange={event => void run('move', { ...initial, dayNo: Number(event.target.value) })}>
        {trip.dayList.map(d => <option key={d.dayNo} value={d.dayNo}>第 {d.dayNo} 天</option>)}
      </select></label>
    </div>
    {fixed && <small>日期、地点与时段已固定；后续 AI 修改须保留。需编辑时先解除，固定不代表平台预订确认。</small>}
    {!fixed && <small>{day.constraintPlanning ? '移动先比较方案，再核对差异并确认保存。已执行与固定项保留。' : '排序保留各项时长与区段间隔；跨天保留时段，冲突会拒绝保存。可撤销。'}</small>}
    {replan && <ReplanOptions trip={trip} result={replan} onSelected={() => { setReplan(null); onDraftCreated?.() }} />}
    {editing && <UserArrangementForm initial={initial} currency={costCurrency} costReadOnly={costReadOnly} days={trip.dayList.map(d => d.dayNo)} busy={locked} onSave={value => void run('update', value)} onCancel={() => setEditing(false)} />}
    {deleting && <div role="group" aria-label={`移除${item.poiName}`}><p>从行程移除「{item.poiName}」？保存后可以就地撤销。</p><button type="button" disabled={locked} onClick={() => void run('delete')}>{day.constraintPlanning ? '比较移除方案' : '确认移除'}</button><button type="button" onClick={() => setDeleting(false)}>取消</button></div>}
    {error && <p role="alert">{error}</p>}
  </div>
}
