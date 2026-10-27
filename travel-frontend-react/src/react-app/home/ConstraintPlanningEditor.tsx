import { useState } from 'react'
import type { TripRequirements } from '../../types/generated/contracts'

export function ConstraintPlanningEditor({ requirements, busy, onChange }: {
  requirements: TripRequirements; days: number; busy: boolean; onChange: (value: TripRequirements) => void
}) {
  const current = requirements.constraintPlanning
  const [country, setCountry] = useState(current?.countryCode || '')
  const [zone, setZone] = useState(current?.timeZone || '')
  const [currency, setCurrency] = useState(current?.currency || '')
  const [evidence, setEvidence] = useState(Boolean(current?.activeEvidence))
  const [sourceSearch, setSourceSearch] = useState(current?.activeEvidence?.allowSourceSearch || false)
  const [error, setError] = useState('')
  const save = () => {
    try { new Intl.DateTimeFormat('zh-CN', { timeZone: zone }).format() } catch { setError('请填写有效的目的地 IANA 时区'); return }
    if (!/^[A-Z]{2}$/.test(country) || !/^[A-Z]{3}$/.test(currency)) { setError('请填写两位国家代码和三位币种代码'); return }
    setError('')
    onChange({ ...requirements, constraintPlanning: { countryCode: country, timeZone: zone, currency,
      activeEvidence: evidence ? { maxQueries: 3, budgetUnits: 4, timeLimitSeconds: 8, allowSourceSearch: sourceSearch, strategy: current?.activeEvidence?.strategy ?? 'fixed' } : null,
    } })
  }
  return <details className="scenario-content">
    <summary>按时间要求安排 · 最长一周</summary>
    <p>在候选地点中安排先后与时段，固定预约优先。营业、交通及费用资料可能仍待核实；无解时展示冲突，不自动放宽要求。</p>
    <p>确认目的地的国家、当地时区和计价币种。预算和已有安排的金额使用该币种；未取得汇率的其他币种费用保留原值，不合计。</p>
    <label>国家代码<input maxLength={2} placeholder="例如 FR" value={country} disabled={busy} onChange={e => setCountry(e.target.value.toUpperCase())} /></label>
    <label>当地时区<input maxLength={64} placeholder="例如 Europe/Paris" value={zone} disabled={busy} onChange={e => setZone(e.target.value.trim())} /></label>
    <label>计价币种<input maxLength={3} placeholder="例如 EUR" value={currency} disabled={busy} onChange={e => setCurrency(e.target.value.toUpperCase())} /></label>
    <label><input type="checkbox" checked={evidence} disabled={busy} onChange={e => setEvidence(e.target.checked)} />优先核实影响安排的资料</label>
    {evidence && <>
      <p>最多补查 3 项，资料足够安排时停止；现有来源无法确认时向你提问。后续比较修改方案也沿用此设置。</p>
      <label><input type="checkbox" checked={sourceSearch} disabled={busy} onChange={e => setSourceSearch(e.target.checked)} />允许联网来源搜索（可能消耗 AI 额度）</label>
    </>}
    <button type="button" className="button button-secondary" disabled={busy} onClick={save}>{current ? '更新目的地口径' : '确认并启用时间约束规划'}</button>
    {current && <><p>已启用：{current.countryCode} · {current.timeZone} · {current.currency}</p><button type="button" className="text-action" disabled={busy} onClick={() => onChange({ ...requirements, constraintPlanning: null })}>使用常规规划</button></>}
    {error && <p role="alert">{error}</p>}
  </details>
}
