import { useState } from 'react'
import { actOnItem } from '../../api/sinan'
import type { DayPlan, ItineraryDetail, TripItem } from '../../types/itinerary'
import type { ItemActionBody } from '../../types/generated/contracts'
import './scenario.css'

export function ItemActions({ trip, day, item, disabled, onChanged }: {
  trip: ItineraryDetail; day: DayPlan; item: TripItem; disabled: boolean; onChanged: (trip: ItineraryDetail) => void
}) {
  const [editing, setEditing] = useState(false)
  const [targetDay, setTargetDay] = useState(day.dayNo)
  const [start, setStart] = useState(item.startTime?.slice(0, 5) || '')
  const [end, setEnd] = useState(item.endTime?.slice(0, 5) || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const fixed = trip.fixedArrangements?.some(f => f.dayNo === day.dayNo && f.name === item.poiName && f.itemType === item.itemType)
  const locked = disabled || busy || item.id === undefined || trip.planningRevision === undefined
  const run = async (action: ItemActionBody['action']) => {
    setBusy(true); setError('')
    try {
      const next = await actOnItem(trip.id, item.id!, { action, planningRevision: trip.planningRevision!, dayNo: targetDay, startTime: start || null, endTime: end || null })
      onChanged(next); setEditing(false); setDeleting(false)
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败，请重试') }
    finally { setBusy(false) }
  }
  return <div className="item-actions" onClick={e => e.stopPropagation()}>
    <div className="item-action-buttons">
      <button type="button" disabled={locked} onClick={() => void run(fixed ? 'unfix' : 'fix')}>{fixed ? '已固定 · 解除' : '固定安排'}</button>
      {item.itemType !== 'hotel' && <><button type="button" disabled={locked || fixed} aria-expanded={editing} onClick={() => { setEditing(!editing); setDeleting(false); setError('') }}>调整时间／日期</button>
        <button type="button" disabled={locked || fixed} onClick={() => { setDeleting(true); setEditing(false); setError('') }}>移除</button></>}
    </div>
    {fixed && <small>日期、地点与时段已固定；后续 AI 修改须保留，非预订确认。</small>}
    {editing && <form className="scenario-fields" onSubmit={e => { e.preventDefault(); void run('update') }}>
      <label>安排到<select value={targetDay} onChange={e => setTargetDay(Number(e.target.value))}>{trip.dayList.map(d => <option key={d.dayNo} value={d.dayNo}>第 {d.dayNo} 天</option>)}</select></label>
      <label>开始时间<input required type="time" value={start} onChange={e => setStart(e.target.value)} /></label>
      <label>结束时间<input required type="time" value={end} onChange={e => setEnd(e.target.value)} /></label>
      <button type="submit" className="button button-primary" disabled={locked}>保存这条安排</button>
      <button type="button" onClick={() => setEditing(false)}>取消</button>
    </form>}
    {deleting && <div role="group" aria-label={`移除${item.poiName}`}><p>从行程移除「{item.poiName}」？保存后可以就地撤销。</p><button type="button" disabled={locked} onClick={() => void run('delete')}>确认移除</button><button type="button" onClick={() => setDeleting(false)}>取消</button></div>}
    {error && <p role="alert">{error}</p>}
  </div>
}
