import { useState } from 'react'
import { actOnItem } from '../../api/sinan'
import type { DayPlan, ItineraryDetail, TripItem } from '../../types/itinerary'
import type { ItemActionBody, ItemCreateBody } from '../../types/generated/contracts'
import { UserArrangementForm, type UserArrangement } from '../shared/UserArrangementForm'
import './scenario.css'

export function ItemActions({ trip, day, item, disabled, onChanged }: {
  trip: ItineraryDetail; day: DayPlan; item: TripItem; disabled: boolean; onChanged: (trip: ItineraryDetail) => void
}) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const fixed = trip.fixedArrangements?.some(f => f.dayNo === day.dayNo && f.name === item.poiName && f.itemType === item.itemType)
  const locked = disabled || busy || item.id === undefined || trip.planningRevision === undefined
  const initial: UserArrangement = { dayNo: day.dayNo, itemType: item.itemType as ItemCreateBody['itemType'], poiName: item.poiName,
    startTime: item.startTime?.slice(0, 5) || '', endTime: item.endTime?.slice(0, 5) || '', address: item.address ?? null, cost: item.cost ?? null, remark: item.remark ?? null, fixed: Boolean(fixed) }
  const run = async (action: ItemActionBody['action'], value = initial) => {
    setBusy(true); setError('')
    try {
      const next = await actOnItem(trip.id, item.id!, { action, planningRevision: trip.planningRevision!, dayNo: value.dayNo,
        startTime: value.startTime || null, endTime: value.endTime || null, poiName: value.poiName,
        address: value.address, cost: value.cost, remark: value.remark })
      onChanged(next); setEditing(false); setDeleting(false)
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败，请重试') }
    finally { setBusy(false) }
  }
  return <div className="item-actions" onClick={e => e.stopPropagation()}>
    <div className="item-action-buttons">
      <button type="button" disabled={locked} onClick={() => void run(fixed ? 'unfix' : 'fix')}>{fixed ? '已固定 · 解除' : '固定安排'}</button>
      <button type="button" disabled={locked || fixed} aria-expanded={editing} onClick={() => { setEditing(!editing); setDeleting(false); setError('') }}>编辑安排</button>
      <button type="button" disabled={locked || fixed} onClick={() => { setDeleting(true); setEditing(false); setError('') }}>移除</button>
    </div>
    {fixed && <small>日期、地点与时段已固定；后续 AI 修改须保留。需编辑时先解除，固定不代表平台预订确认。</small>}
    {editing && <UserArrangementForm initial={initial} days={trip.dayList.map(d => d.dayNo)} busy={locked} onSave={value => void run('update', value)} onCancel={() => setEditing(false)} />}
    {deleting && <div role="group" aria-label={`移除${item.poiName}`}><p>从行程移除「{item.poiName}」？保存后可以就地撤销。</p><button type="button" disabled={locked} onClick={() => void run('delete')}>确认移除</button><button type="button" onClick={() => setDeleting(false)}>取消</button></div>}
    {error && <p role="alert">{error}</p>}
  </div>
}
