import { useState } from 'react'
import { parseScenario } from '../../api/sinan'
import type { ScenarioIntentVO } from '../../types/generated/contracts'
import type { DayPlan, ItineraryDetail } from '../../types/itinerary'

export function ScenarioIntake({ trip, day, disabled, onAdopt }: {
  trip: ItineraryDetail; day: DayPlan; disabled: boolean; onAdopt: (parsed: ScenarioIntentVO) => void
}) {
  const [message, setMessage] = useState('')
  const [result, setResult] = useState<ScenarioIntentVO | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const parse = async () => {
    setBusy(true); setError(''); setResult(null)
    try { setResult(await parseScenario(trip.id, { dayNo: day.dayNo, planningRevision: trip.planningRevision!, message })) }
    catch (err) { setError(err instanceof Error ? err.message : '理解失败，请手动填写') }
    finally { setBusy(false) }
  }
  const names = (ids: number[]) => day.items.filter(p => ids.includes(p.id!)).map(p => p.poiName).join('、') || '未指定'
  return <div className="scenario-intake">
    <label>说说想怎么取舍<textarea value={message} maxLength={2000} disabled={disabled || busy} onChange={e => { setMessage(e.target.value); setResult(null) }} placeholder="例如：16点前结束，草堂时间不动，可以不喝咖啡，转场各留30分钟，返程再留30分钟" /></label>
    <button type="button" className="button button-secondary" disabled={disabled || busy || !message.trim()} onClick={() => void parse()}>{busy ? '正在理解…' : '理解条件，先不比较'}</button>
    {error && <p role="alert">{error}</p>}
    {result && <div className="scenario-hint"><p>识别结果：开始 {result.startTime || '未指定'}；截止 {result.finishBy || '未指定'}；转场 {result.transferMinutes ?? '未指定'} 分钟；返程预留 {result.departureBufferMinutes ?? '未指定'} 分钟。</p>
      <p>本次固定：{names(result.fixedItemIds)}；可舍弃：{names(result.optionalItemIds)}。</p>
      {result.questions.map(q => <p key={q}>{q}</p>)}
      <button type="button" className="button button-secondary" disabled={disabled || busy || result.questions.length > 0} onClick={() => { onAdopt(result); setResult(null) }}>采用这些条件，在下方补齐核对</button>
      <p>未指定的转场和返程预留使用 30 分钟建议值，请手动核对。尚未比较或修改行程。</p>
    </div>}
  </div>
}
