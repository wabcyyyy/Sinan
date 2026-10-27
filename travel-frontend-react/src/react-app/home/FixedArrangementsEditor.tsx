import { useState } from 'react'
import type { TripRequirements } from '../../types/generated/contracts'
import { emptyArrangement, UserArrangementForm } from '../shared/UserArrangementForm'

export function FixedArrangementsEditor({ days, requirements, busy, onChange }: {
  days: number; requirements: TripRequirements; busy: boolean; onChange: (value: TripRequirements) => void
}) {
  const [adding, setAdding] = useState(false)
  const [replaces, setReplaces] = useState('')
  const fixed = requirements.fixedArrangements || []
  return <section className="scenario-content" aria-label="已定安排">
    <h3>已有门票、餐厅或酒店？</h3>
    <p>把确定的地点和时段放进来，司南围绕它们规划。这里只记录你的安排，不替你预订或核验。</p>
    <ul>{fixed.map((f, i) => <li key={`${i}-${f.name}`}>第 {f.dayNo} 天 · {f.startTime}–{f.endTime} · {f.name}
      <button type="button" disabled={busy} onClick={() => onChange({ ...requirements, fixedArrangements: fixed.filter((_, index) => i !== index) })}>移除这条固定安排</button>
    </li>)}</ul>
    <label>住宿房间数（可选）<input type="number" min="1" max="20" value={requirements.lodging?.rooms ?? ''} disabled={busy} onChange={e => onChange({ ...requirements, lodging: { lockedHotelIdentity: null, stayNightsExplicit: false, ...requirements.lodging, rooms: e.target.value ? Number(e.target.value) : null } })} /></label>
    {!adding && <button type="button" className="button button-secondary" disabled={busy || fixed.length >= 40} onClick={() => setAdding(true)}>录入已定安排</button>}
    {adding && <>
      {requirements.requiredPlaces.length > 0 && <label>对应哪条已有必去要求？<select value={replaces} onChange={e => setReplaces(e.target.value)}><option value="">另加安排，不替换已有必去</option>{requirements.requiredPlaces.map(p => <option key={p.constraintId} value={p.constraintId}>{p.name}</option>)}</select><small>若指同一地点，请选中它；保存时将必去名称和日期更新为你填写的内容。</small></label>}
      <UserArrangementForm fixedRequired initial={emptyArrangement(1, true)} days={Array.from({ length: days }, (_, i) => i + 1)} busy={busy} create onCancel={() => setAdding(false)} onSave={value => {
      onChange({ ...requirements, requiredPlaces: requirements.requiredPlaces.map(p => p.constraintId === replaces ? { ...p, name: value.poiName, dayNo: value.dayNo } : p), fixedArrangements: [...fixed, { dayNo: value.dayNo, name: value.poiName, itemType: value.itemType, startTime: value.startTime!, endTime: value.endTime!, cost: value.cost == null ? null : String(value.cost), address: value.address || null, remark: value.remark || null, sourceRef: null }] })
      setAdding(false)
    }} /></>}
  </section>
}
