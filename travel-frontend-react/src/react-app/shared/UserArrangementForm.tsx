import { useState } from 'react'
import type { ItemCreateBody } from '../../types/generated/contracts'
import '../itinerary/scenario.css'

export type UserArrangement = Pick<ItemCreateBody, 'dayNo' | 'itemType' | 'poiName' | 'startTime' | 'endTime' | 'address' | 'cost' | 'remark' | 'fixed'>

/** 用户填写的安排共用一个编辑表单；这里不查询地点，也不声称资料已核验。 */
export function UserArrangementForm({ initial, days, busy, create = false, fixedRequired = false, onSave, onCancel }: {
  initial: UserArrangement; days: number[]; busy: boolean; create?: boolean; fixedRequired?: boolean
  onSave: (value: UserArrangement) => void; onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  const [error, setError] = useState('')
  const patch = (next: Partial<UserArrangement>) => setValue(current => ({ ...current, ...next }))
  return <form className="user-arrangement-form" onSubmit={e => {
    e.preventDefault()
    if (!value.startTime || !value.endTime || value.startTime >= value.endTime) { setError('结束时间须晚于开始时间，暂不支持跨午夜。'); return }
    onSave({ ...value, poiName: value.poiName.trim() })
  }}>
    <fieldset className="scenario-fields" disabled={busy}>
      {create && <label>安排类型<select value={value.itemType} onChange={e => patch({ itemType: e.target.value as UserArrangement['itemType'] })}>
        <option value="attraction">游览／门票</option><option value="food">用餐</option><option value="hotel">住宿入住</option>
      </select></label>}
      <label>地点名称<input required maxLength={128} value={value.poiName} onChange={e => patch({ poiName: e.target.value })} /></label>
      <label>安排到<select value={value.dayNo} onChange={e => patch({ dayNo: Number(e.target.value) })}>{days.map(d => <option key={d} value={d}>第 {d} 天</option>)}</select></label>
      <label>开始时间<input required type="time" value={value.startTime || ''} onChange={e => patch({ startTime: e.target.value })} /></label>
      <label>结束时间<input required type="time" value={value.endTime || ''} onChange={e => patch({ endTime: e.target.value })} /></label>
      <label>单价（元{value.itemType === 'hotel' ? '/间晚' : '/人'}）<input type="number" min="0" max="1000000" step="0.01" value={value.cost ?? ''} placeholder="不知道可留空" onChange={e => patch({ cost: e.target.value === '' ? null : e.target.value })} /></label>
      <label className="arrangement-wide">地址（可选）<input maxLength={500} value={value.address || ''} onChange={e => patch({ address: e.target.value })} /></label>
      <label className="arrangement-wide">备注<textarea maxLength={1000} value={value.remark || ''} placeholder="例如：预约凭证、集合位置、自己的提醒" onChange={e => patch({ remark: e.target.value })} /></label>
      {create && !fixedRequired && <label className="arrangement-wide arrangement-check"><input type="checkbox" checked={value.fixed} onChange={e => patch({ fixed: e.target.checked })} />固定地点和时段，后续 AI 修改须保留</label>}
    </fieldset>
    <p className="scenario-hint">资料由你填写，平台未核验。替换地点会清除旧坐标和来源；时间冲突不会保存。住宿按本行程房间数计价。</p>
    {error && <p role="alert">{error}</p>}
    <div className="item-action-buttons"><button type="submit" className="button button-primary" disabled={busy}>{busy ? '正在保存…' : create ? '添加这条安排' : '保存这条安排'}</button><button type="button" disabled={busy} onClick={onCancel}>取消</button></div>
  </form>
}

export function emptyArrangement(dayNo: number, fixed = false): UserArrangement {
  return { dayNo, itemType: 'attraction', poiName: '', startTime: '', endTime: '', address: '', cost: null, remark: '', fixed }
}
