import { useState } from 'react'
import { compareReplanning, parseReplanChange } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { ReplanOptions, replanChange } from './ReplanOptions'

export function ReplanComposer({ trip, dayNo, selectedItemId, onSelected }: {
  trip: ItineraryDetail; dayNo: number; selectedItemId?: number; onSelected: () => void | Promise<void>
}) {
  const rows = trip.dayList.flatMap(d => d.items.filter(p => p.id !== undefined).map(p => ({ ...p, dayNo: d.dayNo })))
  const existingCompleted = trip.dayList.flatMap(d => d.executedItemIds || [])
  const [itemId, setItemId] = useState(selectedItemId ?? rows.find(p => p.dayNo === dayNo)?.id ?? 0)
  const [kind, setKind] = useState<Contracts.ReplanChange['kind']>('delay')
  const [targetDay, setTargetDay] = useState(dayNo)
  const [start, setStart] = useState('14:00')
  const [finish, setFinish] = useState('16:00')
  const [delay, setDelay] = useState(30)
  const [text, setText] = useState('')
  const [completed, setCompleted] = useState<number[]>(existingCompleted)
  const [optional, setOptional] = useState<number[]>([])
  const [crossDay, setCrossDay] = useState(true)
  const [result, setResult] = useState<Contracts.ReplanCompareVO | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const locked = busy || trip.planningRevision === undefined
  const changed = (fn: () => void) => { fn(); setResult(null); setError('') }
  const run = async (natural: boolean) => {
    setBusy(true); setError(''); setResult(null)
    try {
      let change = replanChange(kind, kind === 'finish_by' ? null : itemId)
      if (['move', 'appointment', 'finish_by'].includes(kind)) change.dayNo = targetDay
      if (['move', 'appointment'].includes(kind)) change.startTime = start || null
      if (['appointment', 'finish_by'].includes(kind)) change.endTime = finish || null
      if (kind === 'delay') change.delayMinutes = delay
      if (natural) {
        const parsed = await parseReplanChange(trip.id, { planningRevision: trip.planningRevision!, dayNo, itemId: itemId || null, message: text })
        if (!parsed.change) { setError(parsed.questions.join('；')); return }
        change = parsed.change
      }
      setResult(await compareReplanning(trip.id, { planningRevision: trip.planningRevision!, change,
        completedItemIds: completed, optionalItemIds: optional, allowCrossDay: crossDay }))
    } catch (err) { setError(err instanceof Error ? err.message : '比较失败，请重试') }
    finally { setBusy(false) }
  }
  return <section className="scenario-content" aria-label="约束行程修改">
    <h3>尽量保留原安排</h3>
    <p>先比较受影响范围，再核对差异并确认。沿用创建时的资料核实设置；启用后优先补查影响安排的资料，未确认的新路段仍标为估算。</p>
    <fieldset disabled={locked} className="scenario-fields">
      <label>要调整的安排<select value={itemId} onChange={e => changed(() => setItemId(Number(e.target.value)))}>{rows.map(p => <option key={p.id} value={p.id}>第 {p.dayNo} 天 · {p.poiName}</option>)}</select></label>
      <label>用一句话描述<input maxLength={2000} value={text} onChange={e => changed(() => setText(e.target.value))} placeholder="推迟30分钟、移到第2天、预约14:00-15:00" /></label>
      <button type="button" className="button button-secondary" disabled={!text.trim()} onClick={() => void run(true)}>理解并比较</button>
    </fieldset>
    <small>免费规则解析只支持列出的单项变化；否定、多地点和歧义会请你确认，不调用模型。</small>
    <details><summary>或手动填写变化与保留范围</summary>
      <fieldset disabled={locked} className="scenario-fields">
        <label>变化类型<select value={kind} onChange={e => changed(() => setKind(e.target.value as Contracts.ReplanChange['kind']))}>
          <option value="delay">延误／推迟</option><option value="move">移动日期或开始时间</option><option value="close">当前活动关闭／取消</option>
          <option value="finish_by">提前结束当天</option><option value="appointment">新增固定预约</option><option value="prefer">优先保留此项（软偏好）</option>
        </select></label>
        {['move', 'appointment', 'finish_by'].includes(kind) && <label>目标日期<select value={targetDay} onChange={e => changed(() => setTargetDay(Number(e.target.value)))}>{trip.dayList.map(d => <option key={d.dayNo} value={d.dayNo}>第 {d.dayNo} 天</option>)}</select></label>}
        {['move', 'appointment'].includes(kind) && <label>开始时间<input type="time" value={start} onChange={e => changed(() => setStart(e.target.value))} /></label>}
        {['appointment', 'finish_by'].includes(kind) && <label>结束／截止时间<input type="time" value={finish} onChange={e => changed(() => setFinish(e.target.value))} /></label>}
        {kind === 'delay' && <label>推迟分钟<input type="number" min={1} max={480} value={delay} onChange={e => changed(() => setDelay(Number(e.target.value)))} /></label>}
        <label><input type="checkbox" checked={crossDay} onChange={e => changed(() => setCrossDay(e.target.checked))} />当前范围无解时，允许扩大到跨天比较</label>
      </fieldset>
      <fieldset disabled={locked} className="scenario-items"><legend>哪些安排必须保留，哪些可以考虑舍弃</legend>
        {rows.map(p => {
          const fixed = trip.fixedArrangements?.some(f => f.dayNo === p.dayNo && f.name === p.poiName && f.itemType === p.itemType)
          return <div key={p.id}><span>第 {p.dayNo} 天 · {p.poiName}</span>
            <label><input type="checkbox" checked={completed.includes(p.id!)} disabled={existingCompleted.includes(p.id!)} onChange={e => changed(() => {
              setCompleted(e.target.checked ? [...completed, p.id!] : completed.filter(id => id !== p.id)); setOptional(optional.filter(id => id !== p.id))
            })} />已执行，保持原样</label>
            <label><input type="checkbox" checked={optional.includes(p.id!)} disabled={Boolean(fixed) || completed.includes(p.id!)} onChange={e => changed(() => setOptional(e.target.checked ? [...optional, p.id!] : optional.filter(id => id !== p.id)))} />可作为舍弃候选</label>
          </div>
        })}
      </fieldset>
      <button type="button" className="button button-secondary" disabled={locked} onClick={() => void run(false)}>比较修改方案</button>
    </details>
    {error && <p role="alert">{error}</p>}
    {result && <ReplanOptions trip={trip} result={result} onSelected={async () => { setResult(null); await onSelected() }} />}
  </section>
}
