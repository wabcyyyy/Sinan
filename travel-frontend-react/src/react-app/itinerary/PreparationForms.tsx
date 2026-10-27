import { useState } from 'react'
import type { SavedPlace, TravelBooking } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'

export const newPlace = (): SavedPlace => ({ id: crypto.randomUUID(), name: '', address: '', note: '', url: '', latitude: null, longitude: null, coordinateSource: null })
export const newBooking = (): TravelBooking => ({ id: crypto.randomUUID(), kind: 'hotel', name: '', startAt: null, endAt: null, reference: '', address: '', note: '', documentUrl: '', itemId: null, inputEvidence: null, timeZone: null, originalAmount: null, originalCurrency: null })

export function PlaceForm({ initial, busy, onSave, onCancel }: { initial: SavedPlace; busy: boolean; onSave: (place: SavedPlace) => void; onCancel: () => void }) {
  const [value, setValue] = useState(initial)
  const patch = (next: Partial<SavedPlace>) => setValue(v => ({ ...v, ...next }))
  return <form className="prep-form" onSubmit={e => { e.preventDefault(); onSave({ ...value, name: value.name.trim() }) }}>
    <fieldset disabled={busy}>
      <label>备选地点名称<input required maxLength={128} value={value.name} onChange={e => patch({ name: e.target.value })} /></label>
      <label>地址<input maxLength={255} value={value.address} onChange={e => patch({ address: e.target.value })} /></label>
      <label>参考链接<input type="url" maxLength={2048} placeholder="https://" value={value.url} onChange={e => patch({ url: e.target.value })} /></label>
      <label>想去的理由<textarea maxLength={1000} value={value.note} onChange={e => patch({ note: e.target.value })} /></label>
      {value.latitude !== null && <small>地图手动落点 · WGS84 {value.latitude.toFixed(5)}, {value.longitude?.toFixed(5)}<button type="button" onClick={() => patch({ latitude: null, longitude: null, coordinateSource: null })}>清除落点</button></small>}
    </fieldset>
    <div className="prep-actions"><button type="submit" disabled={busy}>保存备选</button><button type="button" disabled={busy} onClick={onCancel}>取消</button></div>
  </form>
}

export function BookingForm({ initial, trip, busy, onSave, onCancel }: { initial: TravelBooking; trip: ItineraryDetail; busy: boolean; onSave: (booking: TravelBooking) => void; onCancel: () => void }) {
  const [value, setValue] = useState(initial)
  const [error, setError] = useState('')
  const patch = (next: Partial<TravelBooking>) => setValue(v => ({ ...v, ...next }))
  return <form className="prep-form" onSubmit={e => {
    e.preventDefault()
    if (value.startAt && value.endAt && value.endAt <= value.startAt) { setError('结束须晚于开始，跨夜住宿请选次日日期'); return }
    onSave({ ...value, name: value.name.trim() })
  }}>
    <fieldset disabled={busy}>
      <label>资料类型<select value={value.kind} onChange={e => patch({ kind: e.target.value as TravelBooking['kind'] })}><option value="hotel">住宿</option><option value="transport">交通</option><option value="ticket">门票／预约</option><option value="other">其他</option></select></label>
      <label>预订名称<input required maxLength={128} value={value.name} onChange={e => patch({ name: e.target.value })} /></label>
      <label>开始／入住<input type="datetime-local" value={value.startAt?.slice(0, 16) || ''} onChange={e => patch({ startAt: e.target.value || null, endAt: value.endAt?.slice(0, 16) || null, inputEvidence: null })} /></label>
      <label>结束／退房<input type="datetime-local" value={value.endAt?.slice(0, 16) || ''} onChange={e => patch({ endAt: e.target.value || null, startAt: value.startAt?.slice(0, 16) || null, inputEvidence: null })} /></label>
      {value.timeZone && <p>当地时区：{value.timeZone}；手动调整时间后原截图字段确认已撤销。</p>}
      <label>确认号<input maxLength={128} value={value.reference} onChange={e => patch({ reference: e.target.value })} /></label>
      <label>地址／起讫信息<input maxLength={255} value={value.address} onChange={e => patch({ address: e.target.value })} /></label>
      <label>凭证链接<input type="url" maxLength={2048} value={value.documentUrl} onChange={e => patch({ documentUrl: e.target.value })} /></label>
      <label>关联行程安排<select value={value.itemId ?? ''} onChange={e => patch({ itemId: e.target.value ? Number(e.target.value) : null })}><option value="">暂不关联</option>{trip.dayList.flatMap(d => d.items.filter(p => p.id).map(p => <option key={p.id} value={p.id}>第{d.dayNo}天 · {p.poiName} · {p.startTime?.slice(0, 5)}</option>))}</select></label>
      <label>备注／确认文字<textarea maxLength={2000} value={value.note} onChange={e => patch({ note: e.target.value })} /></label>
    </fieldset>
    <p className="prep-caption">按预订当地时间填写。此处保存你的资料；关联后可另行固定已有安排的日期、地点和时段。跨夜资料不会伪装成同日活动。</p>
    {error && <p role="alert">{error}</p>}
    <div className="prep-actions"><button type="submit" disabled={busy}>保存已订资料</button><button type="button" disabled={busy} onClick={onCancel}>取消</button></div>
  </form>
}
