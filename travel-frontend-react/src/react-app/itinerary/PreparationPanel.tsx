import { useEffect, useState } from 'react'
import { createUserItem, fixBooking } from '../../api/sinan'
import type { PreparationTask, SavedPlace, TravelBooking, UserConfirmation } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { emptyArrangement, UserArrangementForm } from '../shared/UserArrangementForm'
import { BookingForm, newBooking, newPlace, PlaceForm } from './PreparationForms'
import { PlanningBudget } from './PlanningBudget'
import { ScreenshotImport } from '../shared/ScreenshotImport'
import { externalLink, type PreparationController } from './usePreparation'
import './preparation.css'

const sections = { places: '备选地点', bookings: '已订资料', tasks: '行前清单', confirmations: '用户核实', budget: '规划预算' }
const aspects = { location: '地点与位置', opening: '开放／预约要求', price: '费用口径', booking: '预订状态' }
const bookingKinds = { hotel: '住宿', transport: '交通', ticket: '门票／预约', other: '其他' }

export function PreparationPanel({ trip, controller, seed, onChanged, readOnly = false }: {
  trip: ItineraryDetail; controller: PreparationController; seed?: SavedPlace; onChanged: (trip: ItineraryDetail) => void; readOnly?: boolean
}) {
  const [section, setSection] = useState<keyof typeof sections>('places')
  const [place, setPlace] = useState<SavedPlace | null>(null)
  const [booking, setBooking] = useState<TravelBooking | null>(null)
  const [arranging, setArranging] = useState<SavedPlace | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [taskTitle, setTaskTitle] = useState('')
  const [category, setCategory] = useState<PreparationTask['category']>('reservation')
  const [taskDay, setTaskDay] = useState<number | null>(null)
  const [itemId, setItemId] = useState('')
  const [aspect, setAspect] = useState<UserConfirmation['aspect']>('opening')
  const [note, setNote] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const { data, save } = controller
  const locked = readOnly || busy || controller.busy || !controller.state
  const items = trip.dayList.flatMap(d => d.items.map(item => ({ ...item, dayNo: d.dayNo })))
  useEffect(() => { if (seed) { setPlace(seed); setSection('places') } }, [seed])
  const putPlace = async (next: SavedPlace) => {
    if (await save({ ...data, places: [...data.places.filter(p => p.id !== next.id), next] })) setPlace(null)
  }
  const putBooking = async (next: TravelBooking) => {
    if (await save({ ...data, bookings: [...data.bookings.filter(p => p.id !== next.id), next] })) setBooking(null)
  }
  return <section className="preparation-panel" aria-label="旅行准备资料">
    <header><span className="section-eyebrow">为出发做好准备</span><h2>旅行资料</h2><p className="prep-caption">私有资料，仅本人可见；公开路线和分享不会包含这里的确认号、凭证和备注。{readOnly ? '当前只读。' : ''}</p></header>
    <nav className="prep-tabs" aria-label="资料分类">{Object.entries(sections).map(([key, label]) => <button type="button" key={key} aria-pressed={section === key} onClick={() => setSection(key as keyof typeof sections)}>{label}{key === 'tasks' && ` ${data.tasks.filter(t => !t.done).length}`}</button>)}</nav>
    {(controller.error || error) && <div className="prep-error" role="alert">{controller.error || error}<button type="button" onClick={() => { setError(''); void controller.reload() }}>重新载入资料</button></div>}
    {!controller.state && !controller.error && section !== 'budget' && <p role="status">正在读取旅行资料…</p>}
    {section === 'places' && <>
      <p className="prep-caption">先收集想去的地方，决定日期后再安排。也可以在地图上手动落点。</p>
      {!place && !readOnly && <button className="button button-secondary" type="button" disabled={locked} onClick={() => setPlace(newPlace())}>＋ 收下一个备选</button>}
      {place && <PlaceForm key={place.id} initial={place} busy={locked} onSave={p => void putPlace(p)} onCancel={() => setPlace(null)} />}
      {data.places.length === 0 && !place && <p className="prep-empty">留一处还没决定的去处，给旅程多一种选择。</p>}
      <ul className="prep-list">{data.places.map(p => <li key={p.id}><h3>{p.name}</h3><p>{p.address}</p><p>{p.note}</p>{externalLink(p.url) && <a href={externalLink(p.url)} target="_blank" rel="noreferrer">查看参考链接 ↗</a>}{p.coordinateSource && <small>用户地图落点 · 尚未核实地点身份</small>}
        {!readOnly && <div className="prep-actions"><button type="button" disabled={locked || trip.status !== 2} onClick={() => { setArranging(p); setError('') }}>安排到某天</button><button type="button" disabled={locked} onClick={() => setPlace(p)}>编辑</button><button type="button" disabled={locked} onClick={() => void save({ ...data, places: data.places.filter(row => row.id !== p.id) })}>移出备选</button></div>}
        {arranging?.id === p.id && <UserArrangementForm initial={{ ...emptyArrangement(1), poiName: p.name, address: p.address, remark: p.note.slice(0, 255) }} create days={trip.dayList.map(d => d.dayNo)} busy={locked} onCancel={() => setArranging(null)} onSave={async value => {
          setBusy(true); setError('')
          try { onChanged(await createUserItem(trip.id, { ...value, action: 'create', planningRevision: trip.planningRevision!, latitude: p.latitude, longitude: p.longitude })); setArranging(null) }
          catch (err) { setError(err instanceof Error ? err.message : '安排失败') } finally { setBusy(false) }
        }} />}
      </li>)}</ul>
    </>}
    {section === 'bookings' && <>
      <p className="prep-caption">我已订好 · 用户自行保管与确认，不代表平台出票或官方核验。</p>
      {!readOnly && trip.requirementsStruct?.constraintPlanning && trip.days <= 3 && <ScreenshotImport bookingOnly requirements={trip.requirementsStruct} city={trip.city} days={trip.days} startDate={trip.startDate} busy={locked} onAdopt={async (_requirements, next) => next ? save({ ...data, bookings: [...data.bookings.filter(b => b.id !== next.id), next] }) : false} />}
      {!booking && !readOnly && <button className="button button-secondary" type="button" disabled={locked} onClick={() => setBooking(newBooking())}>＋ 添加已订资料</button>}
      {booking && <BookingForm key={booking.id} initial={booking} trip={trip} busy={locked} onSave={b => void putBooking(b)} onCancel={() => setBooking(null)} />}
      {!data.bookings.length && !booking && <p className="prep-empty">把酒店确认号、车次起讫和预约信息放在一起，出门时快速找到。</p>}
      <ul className="prep-list">{data.bookings.map(b => {
        const linked = items.find(p => p.id === b.itemId)
        const fixed = linked && trip.fixedArrangements?.some(f => f.dayNo === linked.dayNo && f.name === linked.poiName && f.itemType === linked.itemType)
        return <li key={b.id}><small>{bookingKinds[b.kind]} · 用户已订记录</small><h3>{b.name}</h3><p>{b.startAt?.replace('T', ' ')}{b.endAt && ` → ${b.endAt.replace('T', ' ')}`}</p>{b.timeZone && <p>当地时区 {b.timeZone}</p>}{b.originalAmount != null && <p>原币金额 {b.originalCurrency || '币种待确认'} {b.originalAmount} · 未自动换算</p>}{b.inputEvidence && <details><summary>截图字段来源（用户确认）</summary><ul>{b.inputEvidence.fields.map(f => <li key={f.key}>{f.key}：{f.value} · 原文「{f.quote}」</li>)}</ul><small>原图未保存；后续可重新上传核对区域，不代表官方核验。</small></details>}{b.reference && <p>确认号 <strong>{b.reference}</strong></p>}<p>{b.address}</p><p className="prep-preserve">{b.note}</p>{externalLink(b.documentUrl) && <a href={externalLink(b.documentUrl)} target="_blank" rel="noreferrer">打开凭证 ↗</a>}
          {linked ? <p>关联第 {linked.dayNo} 天 · {linked.poiName} · {linked.startTime?.slice(0, 5)}–{linked.endTime?.slice(0, 5)}{fixed && ' · 已固定'}</p> : b.itemId ? <p className="prep-unknown">原关联安排已变化，请重新关联。</p> : null}
          {!readOnly && <div className="prep-actions"><button disabled={locked} onClick={() => setBooking(b)}>编辑资料</button>{linked && !fixed && <button disabled={locked || trip.status !== 2} onClick={async () => {
            setBusy(true); setError('')
            try { onChanged(await fixBooking(trip.id, b.id, trip.planningRevision!)) }
            catch (err) { setError(err instanceof Error ? err.message : '固定失败') } finally { setBusy(false) }
          }}>固定关联安排</button>}<button disabled={locked} onClick={() => void save({ ...data, bookings: data.bookings.filter(row => row.id !== b.id) })}>移除资料</button></div>}
        </li>
      })}</ul>
    </>}
    {section === 'tasks' && <>
      <h3>还有 {data.tasks.filter(t => !t.done).length} 件事，出发前处理</h3>
      {!readOnly && <form className="prep-task-form" onSubmit={async e => { e.preventDefault(); if (await save({ ...data, tasks: [...data.tasks, { id: crypto.randomUUID(), title: taskTitle.trim(), category, dayNo: taskDay, done: false }] })) setTaskTitle('') }}>
        <label>行前事项<input required maxLength={200} value={taskTitle} placeholder="例如：确认博物馆预约" onChange={e => setTaskTitle(e.target.value)} /></label>
        <label>类别<select value={category} onChange={e => setCategory(e.target.value as PreparationTask['category'])}><option value="reservation">预约</option><option value="transport">交通</option><option value="stay">住宿</option><option value="documents">证件</option><option value="packing">行李</option><option value="other">其他</option></select></label>
        <label>关联日期<select value={taskDay ?? ''} onChange={e => setTaskDay(e.target.value ? Number(e.target.value) : null)}><option value="">整趟旅行</option>{trip.dayList.map(d => <option key={d.dayNo} value={d.dayNo}>第 {d.dayNo} 天</option>)}</select></label><button disabled={locked}>添加事项</button>
      </form>}
      <ul className="prep-list">{data.tasks.map(t => <li key={t.id} className="prep-task"><label><input type="checkbox" checked={t.done} disabled={locked} onChange={() => void save({ ...data, tasks: data.tasks.map(row => row.id === t.id ? { ...row, done: !row.done } : row) })} /><span>{t.title}{t.dayNo && <small>第 {t.dayNo} 天</small>}</span></label>{!readOnly && <button disabled={locked} onClick={() => void save({ ...data, tasks: data.tasks.filter(row => row.id !== t.id) })} aria-label={`移除事项${t.title}`}>移除</button>}</li>)}</ul>
    </>}
    {section === 'confirmations' && <>
      <p className="prep-caption">记录你已查过的具体事实和来源，不改写平台核验标识。打开链接不会自动记为确认。</p>
      {!readOnly && <form className="prep-form" onSubmit={async e => {
        e.preventDefault(); const item = items.find(p => p.id === Number(itemId)); if (!item) return
        const next: UserConfirmation = { id: crypto.randomUUID(), itemId: item.id!, name: item.poiName, aspect, note: note.trim(), sourceUrl, recordedAt: null }
        if (await save({ ...data, confirmations: [...data.confirmations.filter(c => !(c.itemId === next.itemId && c.aspect === next.aspect)), next] })) { setNote(''); setSourceUrl('') }
      }}><fieldset disabled={locked}>
        <label>我核实了哪项<select required value={itemId} onChange={e => setItemId(e.target.value)}><option value="">选择当前安排</option>{items.filter(p => p.id).map(p => <option key={p.id} value={p.id}>第{p.dayNo}天 · {p.poiName}</option>)}</select></label>
        <label>核实方面<select value={aspect} onChange={e => setAspect(e.target.value as UserConfirmation['aspect'])}>{Object.entries(aspects).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
        <label>核实结果<textarea required maxLength={1000} value={note} placeholder="例如：已在官网确认周日开放，预约成功" onChange={e => setNote(e.target.value)} /></label>
        <label>来源链接（可选）<input type="url" maxLength={2048} value={sourceUrl} onChange={e => setSourceUrl(e.target.value)} /></label>
      </fieldset><button disabled={locked}>记录用户已确认</button></form>}
      <ul className="prep-list">{data.confirmations.map(c => <li key={c.id}><small>{items.some(p => p.id === c.itemId && p.poiName === c.name) ? '用户已确认' : '原安排已变化 · 历史记录'} · {aspects[c.aspect]}</small><h3>{c.name}</h3><p>{c.note}</p><small>{c.recordedAt ? new Date(c.recordedAt).toLocaleString('zh-CN') : '尚无记录时间'}</small>{externalLink(c.sourceUrl) && <a href={externalLink(c.sourceUrl)} target="_blank" rel="noreferrer">核实来源 ↗</a>}{!readOnly && <button disabled={locked} onClick={() => void save({ ...data, confirmations: data.confirmations.filter(row => row.id !== c.id) })}>撤回记录</button>}</li>)}</ul>
    </>}
    {section === 'budget' && <PlanningBudget trip={trip} />}
    {controller.state?.updatedAt && <p className="prep-caption">资料最近保存 {new Date(controller.state.updatedAt).toLocaleString('zh-CN')} · 修订 {controller.state.revision}</p>}
  </section>
}
