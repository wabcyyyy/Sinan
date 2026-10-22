import { useState } from 'react'
import { compareScenarios, selectScenario } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { DayPlan, ItineraryDetail } from '../../types/itinerary'
import { isDemoMode } from '../../demoMode'
import './scenario.css'
import { ScenarioIntake } from './ScenarioIntake'

/** 父组件以行程修订号和日期为 key；切日或保存后不复用旧比较。 */
export function ScenarioPanel({ trip, day, disabled, onSelected }: {
  trip: ItineraryDetail; day: DayPlan; disabled: boolean; onSelected: () => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [start, setStart] = useState('09:30')
  const [finish, setFinish] = useState('16:00')
  const [transfer, setTransfer] = useState(30)
  const [buffer, setBuffer] = useState(30)
  const persistentIds = day.items.filter(p => trip.fixedArrangements?.some(f => f.dayNo === day.dayNo && f.name === p.poiName && f.itemType === p.itemType)).flatMap(p => p.id === undefined ? [] : [p.id])
  const [fixed, setFixed] = useState<number[]>(persistentIds)
  const [optional, setOptional] = useState<number[]>([])
  const [result, setResult] = useState<Contracts.ScenarioCompareVO | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const demo = isDemoMode()
  const locked = busy || disabled || trip.planningRevision === undefined
  const body: Contracts.ScenarioBody = { dayNo: day.dayNo, planningRevision: trip.planningRevision ?? 0,
    startTime: start, finishBy: finish, transferMinutes: transfer, departureBufferMinutes: buffer,
    fixedItemIds: fixed, optionalItemIds: optional }
  const change = (fn: () => void) => { fn(); setResult(null); setError('') }
  const run = async (choice?: Contracts.ScenarioSelectBody['choice']) => {
    setBusy(true); setError('')
    try {
      if (choice) { await selectScenario(trip.id, { ...body, choice }); await onSelected(); setOpen(false) }
      else setResult(await compareScenarios(trip.id, body))
    } catch (err) { setError(err instanceof Error ? err.message : '比较失败，请重试') }
    finally { setBusy(false) }
  }
  return <section className="scenario-panel">
    <button className="button button-secondary" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>时间不够？比较两种取舍</button>
    {open && <div className="scenario-content">
      <h3>先保住确定的安排，再决定舍弃什么</h3>
      {!demo && <ScenarioIntake trip={trip} day={day} disabled={locked} onAdopt={parsed => change(() => {
        setStart(parsed.startTime ?? ''); setFinish(parsed.finishBy ?? ''); setTransfer(parsed.transferMinutes ?? 30); setBuffer(parsed.departureBufferMinutes ?? 30)
        setFixed([...new Set([...persistentIds, ...parsed.fixedItemIds])]); setOptional(parsed.optionalItemIds.filter(id => !persistentIds.includes(id)))
      })} />}
      <p>只调整第 {day.dayNo} 天已有项目的时间，保留顺序与时长。固定项保持原时段，住宿与其他日期不变。这里新增的固定仅对本次调整生效；条目上已固定的安排持续生效，需先在条目上解除。</p>
      {demo && <p className="scenario-hint">固定样例演示：请在未修改的成都第 2 天载入示例。此处不调用模型，不支持任意条件计算。
        <button type="button" className="button button-secondary" disabled={locked || day.dayNo !== 2} onClick={() => change(() => { setStart('09:30'); setFinish('16:00'); setTransfer(30); setBuffer(30); setFixed([21]); setOptional([24, 25]) })}>载入返程取舍示例</button></p>}
      <fieldset disabled={locked || demo} className="scenario-fields">
        <label>最早开始<input type="time" value={start} onChange={e => change(() => setStart(e.target.value))} /></label>
        <label>返程／截止时间<input type="time" value={finish} onChange={e => change(() => setFinish(e.target.value))} /></label>
        <label>每次转场预留（分钟）<input type="number" min={0} max={240} value={transfer} onChange={e => change(() => setTransfer(Number(e.target.value)))} /></label>
        <label>返程额外预留（分钟）<input type="number" min={0} max={480} value={buffer} onChange={e => change(() => setBuffer(Number(e.target.value)))} /></label>
      </fieldset>
      <p className="scenario-hint">转场和返程预留由你填写，不是实时交通测算；既有时间要求不会被放宽。</p>
      <fieldset disabled={locked || demo} className="scenario-items"><legend>逐项选择</legend>
        {day.items.map((item, index) => <label key={item.id ?? index}><span>{item.startTime?.slice(0, 5) || '时间待补'} · {item.poiName}</span>
          <select aria-label={`${item.poiName}的安排方式`} disabled={item.id === undefined || item.itemType === 'hotel' || persistentIds.includes(item.id)} value={item.itemType === 'hotel' || fixed.includes(item.id!) ? 'fixed' : optional.includes(item.id!) ? 'optional' : 'flex'}
            onChange={e => change(() => { setFixed([...fixed.filter(id => id !== item.id), ...(e.target.value === 'fixed' ? [item.id!] : [])]); setOptional([...optional.filter(id => id !== item.id), ...(e.target.value === 'optional' ? [item.id!] : [])]) })}>
            <option value="flex">保留，可调整时间</option><option value="fixed">固定原时段</option><option value="optional">可以舍弃</option>
          </select></label>)}
      </fieldset>
      <button className="button button-primary" type="button" disabled={locked || !start || !finish} onClick={() => void run()}>{busy ? '正在处理…' : '比较方案，不修改行程'}</button>
      {error && <p role="alert">{error}</p>}
      {result && <><p className="scenario-hint">选择会替换当前待确认草稿，正式行程要到下一步确认后才改变。</p><div className="scenario-options">{result.options.map(option => <article key={option.key}>
        <h4>{option.title}</h4><strong>{option.feasible ? '满足本次时间与已有规则' : '存在冲突，不能应用'}</strong>
        <p>结束 {option.finishTime ?? '无法计算'} · {option.slackMinutes === null ? '余量未知' : option.slackMinutes >= 0 ? `剩余 ${option.slackMinutes} 分钟` : `超时 ${-option.slackMinutes} 分钟`}</p>
        <p>条目费用差额：{option.costDelta === null ? '缺少可比价格' : `￥${option.costDelta}`}（沿用原估价，非报价）</p>
        {option.conflicts.length > 0 && <ul>{option.conflicts.map(reason => <li key={reason}>{reason}</li>)}</ul>}
        <ol>{option.items.map(item => <li key={item.id}>{item.startTime?.slice(0, 5)}–{item.endTime?.slice(0, 5)} {item.name}{item.fixed ? ' · 固定' : ''}</li>)}</ol>
        <p>舍弃：{option.removed.join('、') || '无'}</p>
        <button className="button button-secondary" type="button" disabled={locked || !option.feasible} onClick={() => void run(option.key)}>选择此版，查看确认差异</button>
      </article>)}</div><ul className="scenario-hint">{result.assumptions.map(text => <li key={text}>{text}</li>)}</ul></>}
    </div>}
  </section>
}
