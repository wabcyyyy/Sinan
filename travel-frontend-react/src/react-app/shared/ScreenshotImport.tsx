import { useEffect, useState } from 'react'
import { confirmScreenshot, extractScreenshot, resolveScreenshot, registerManualScreenshotPlace } from '../../api/sinan'
import type { EvidenceRegion, ScreenshotConfirmBody, ScreenshotDocumentVO, ScreenshotEntry, ScreenshotResolveVO, TravelBooking, TripRequirements } from '../../types/generated/contracts'
import './screenshot.css'

type EditFields = { name: string; city: string; countryCode: string; localDate: string; endDate: string; startTime: string; endTime: string; timeZone: string; amount: string; currency: string }

function fieldsOf(entry: ScreenshotEntry, city: string, countryCode: string): EditFields {
  const fields = Object.fromEntries(entry.fields.map(f => [f.key, f.value]))
  return { name: fields.name || fields.localName || '', city: fields.city || city, countryCode: fields.countryCode || countryCode,
    localDate: fields.date || '', endDate: fields.endDate || '', startTime: fields.startTime || '', endTime: fields.endTime || '',
    timeZone: fields.timeZone || '', amount: fields.amount || '', currency: fields.currency || '' }
}

export function ScreenshotImport({ requirements, city, days, startDate, busy = false, bookingOnly = false, onAdopt }: {
  requirements: TripRequirements; city: string; days: number; startDate?: string | null; busy?: boolean; bookingOnly?: boolean
  onAdopt: (requirements: TripRequirements, booking: TravelBooking | null) => void | Promise<boolean | void>
}) {
  const [kind, setKind] = useState<'guide' | 'booking'>('guide')
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState('')
  const [document, setDocument] = useState<ScreenshotDocumentVO | null>(null)
  const [index, setIndex] = useState(0)
  const [edit, setEdit] = useState<EditFields | null>(null)
  const [resolution, setResolution] = useState<ScreenshotResolveVO | null>(null)
  const [entityId, setEntityId] = useState('')
  const [disposition, setDisposition] = useState<ScreenshotConfirmBody['disposition']>('candidate')
  const [overridePolarity, setOverride] = useState(false)
  const [region, setRegion] = useState<EvidenceRegion | null>(null)
  const [working, setWorking] = useState(false)
  const [notice, setNotice] = useState('')
  const [manualAddress, setManualAddress] = useState('')
  const [manualLat, setManualLat] = useState('')
  const [manualLon, setManualLon] = useState('')
  const [manualType, setManualType] = useState<'unconfirmed' | 'museum' | 'station' | 'airport' | 'bus_station' | 'ferry_terminal'>('unconfirmed')
  const locked = busy || working
  const entry = document?.entries[index]
  const needsPolarityOverride = entry?.polarity === 'unclear' || (entry?.polarity === 'excluded' && disposition !== 'excluded')
  useEffect(() => {
    if (!file) { setPreview(''); return }
    const url = URL.createObjectURL(file); setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const chooseEntry = (doc: ScreenshotDocumentVO, next: number) => {
    const row = doc.entries[next]
    setIndex(next); setResolution(null); setEntityId(''); setOverride(false); setRegion(null)
    setManualAddress(''); setManualLat(''); setManualLon(''); setManualType('unconfirmed')
    if (!row) { setEdit(null); return }
    setEdit(fieldsOf(row, city, requirements.constraintPlanning?.countryCode || ''))
    setDisposition(bookingOnly ? 'booking' : row.polarity === 'excluded' ? 'excluded' : row.kind === 'place' ? 'candidate' : 'fixed')
  }
  const change = (key: keyof EditFields, value: string) => {
    setEdit(current => current && ({ ...current, [key]: value }))
    if (['name', 'city', 'countryCode'].includes(key)) { setResolution(null); setEntityId('') }
  }
  const extract = async () => {
    if (!file || locked) return
    setWorking(true); setNotice(''); setDocument(null); setResolution(null); setEdit(null)
    try { const doc = await extractScreenshot(file, bookingOnly ? 'booking' : kind); setDocument(doc); chooseEntry(doc, 0) }
    catch (err) { setNotice(err instanceof Error ? err.message : '截图识别失败') }
    finally { setWorking(false) }
  }
  const resolve = async () => {
    if (!document || !entry || !edit || locked) return
    setWorking(true); setNotice(''); setEntityId(''); setResolution(null)
    try { setResolution(await resolveScreenshot({ documentId: document.documentId, recordId: entry.recordId, name: edit.name, city: edit.city, countryCode: edit.countryCode })) }
    catch (err) { setNotice(err instanceof Error ? err.message : '地点查询失败') }
    finally { setWorking(false) }
  }
  const adopt = async () => {
    if (!document || !entry || !edit || !resolution || !entityId || locked) return
    setWorking(true); setNotice('')
    try {
      const result = await confirmScreenshot({ documentId: document.documentId, recordId: entry.recordId,
        resolutionId: resolution.resolutionId, entityId, disposition, requirements, days, startDate: startDate || null,
        localDate: edit.localDate || null, endDate: edit.endDate || null, startTime: edit.startTime || null, endTime: edit.endTime || null,
        timeZone: edit.timeZone || null, amount: edit.amount || null, currency: edit.currency || null, overridePolarity })
      const saved = await onAdopt(result.requirements, result.booking)
      if (saved !== false) { setNotice(bookingOnly ? '已保存到私有已订资料；正式行程未变更。' : result.notice); setEntityId('') }
      else setNotice('资料保存未完成，请检查原资料面板的错误并重新载入。')
    } catch (err) { setNotice(err instanceof Error ? err.message : '截图确认失败') }
    finally { setWorking(false) }
  }

  const manual = async () => {
    if (!document || !entry || !edit || locked) return
    setWorking(true); setNotice(''); setEntityId('')
    try {
      const result = await registerManualScreenshotPlace({ documentId: document.documentId, recordId: entry.recordId,
        name: edit.name, city: edit.city, countryCode: edit.countryCode, address: manualAddress,
        latitude: manualLat === '' ? null : Number(manualLat), longitude: manualLon === '' ? null : Number(manualLon), entityType: manualType })
      setResolution(result)
    } catch (err) { setNotice(err instanceof Error ? err.message : '手工地点未登记') }
    finally { setWorking(false) }
  }

  return <details className="screenshot-import">
    <summary>{bookingOnly ? '从车票或预约截图录入资料' : '从攻略、车票或预约截图补充需求'}</summary>
    <p>图片会发送至你配置的视觉服务。请遮挡姓名、订单号和二维码；原图仅在本页预览。识别结果需逐项确认，作者推荐不会自动成为必去。</p>
    {!requirements.constraintPlanning && <p role="status">先在约束规划中确认国家、当地时区和计价币种，再采用截图。</p>}
    <fieldset disabled={locked}>
      {!bookingOnly && <label>截图类型<select value={kind} onChange={e => { setKind(e.target.value as 'guide' | 'booking'); setDocument(null); setEdit(null); setResolution(null); setEntityId('') }}><option value="guide">攻略／推荐</option><option value="booking">车票／预约</option></select></label>}
      <label>选择截图<input type="file" accept="image/png,image/jpeg,image/webp" onChange={e => {
        setFile(e.target.files?.[0] || null); setDocument(null); setEdit(null); setResolution(null); setEntityId(''); setNotice('')
      }} /></label>
      <button type="button" className="button button-secondary" disabled={!file} onClick={() => void extract()}>{working ? '处理中…' : '识别截图字段'}</button>
    </fieldset>
    {preview && <figure className="screenshot-preview"><img src={preview} alt="本次上传截图，仅当前页面预览" />{region && <span className="screenshot-region" aria-label="当前字段证据区域" style={{ left: `${region.x * 100}%`, top: `${region.y * 100}%`, width: `${region.width * 100}%`, height: `${region.height * 100}%` }} />}</figure>}
    {document && <>
      {document.questions.map((question, i) => <p key={i}>{question}</p>)}
      <nav aria-label="截图记录">{document.entries.map((row, i) => <button type="button" key={row.recordId} disabled={locked} aria-pressed={i === index} onClick={() => chooseEntry(document, i)}>记录 {i + 1}</button>)}</nav>
      {!document.entries.length && <p role="status">未取得可用字段，请换清晰图片或手工录入。</p>}
    </>}
    {entry && edit && <>
      <p>原图语义：{entry.polarity === 'excluded' ? '明确排除' : entry.polarity === 'recommended' ? '推荐／正向输入' : '待确认'}。区域由识别模型给出，请对照原图核实。</p>
      <ul className="screenshot-fields">{entry.fields.map(field => <li key={field.key}><button type="button" onClick={() => setRegion(field.region)}>{field.key}：{field.value}</button><q>{field.quote}</q>{field.language && <small>{field.language}</small>}</li>)}</ul>
      {entry.questions.map((question, i) => <p key={i}>{question}</p>)}
      <fieldset disabled={locked}>
        <label>地点名称／馆区<input maxLength={64} value={edit.name} onChange={e => change('name', e.target.value)} /></label>
        <label>所在城市<input maxLength={80} value={edit.city} onChange={e => change('city', e.target.value)} /></label>
        <label>国家代码<input maxLength={2} value={edit.countryCode} placeholder="FR / JP / CN" onChange={e => change('countryCode', e.target.value.toUpperCase())} /></label>
        <button type="button" disabled={!edit.name || !edit.city || !/^[A-Z]{2}$/.test(edit.countryCode)} onClick={() => void resolve()}>查询地点实体</button>
      </fieldset>
      {resolution && <fieldset disabled={locked}><legend>确认具体地点</legend><p>{resolution.question}</p>{resolution.choices.map(choice => <label key={choice.entityId}>
        <input type="radio" name={`entity-${document?.documentId}`} checked={entityId === choice.entityId} value={choice.entityId} onChange={() => setEntityId(choice.entityId)} />
        {choice.name} · {choice.address}<small>{choice.aliases.join(' / ')} · {choice.category}/{choice.entityType} · {choice.countryCode} · {choice.identitySource === 'user_provided' ? '手工输入，未核验' : '地图候选'} · {choice.matchReasons?.join('；')}</small>
        {choice.sourceUrl && <a href={choice.sourceUrl} target="_blank" rel="noreferrer">查看地图实体</a>}
      </label>)}</fieldset>}
      {resolution && resolution.status !== 'choices' && <fieldset disabled={locked}>
        <legend>手工补充地点</legend>
        <p>可保留地点和截图，坐标留空则待定位；这不会核实营业时间或票价。</p>
        <label>手工地址<input value={manualAddress} maxLength={255} onChange={e => setManualAddress(e.target.value)} /></label>
        <label>纬度（可留空）<input type="number" value={manualLat} onChange={e => setManualLat(e.target.value)} /></label>
        <label>经度（可留空）<input type="number" value={manualLon} onChange={e => setManualLon(e.target.value)} /></label>
        <label>地点类别<select value={manualType} onChange={e => setManualType(e.target.value as typeof manualType)}>
          <option value="unconfirmed">待确认</option><option value="museum">博物馆</option><option value="station">车站</option><option value="airport">机场</option><option value="bus_station">汽车站</option><option value="ferry_terminal">码头</option>
        </select></label>
        <button type="button" disabled={!manualAddress.trim()} onClick={() => void manual()}>登记手工地点</button>
      </fieldset>}
      <fieldset disabled={locked}>
        <label>采用方式<select value={disposition} onChange={e => setDisposition(e.target.value as ScreenshotConfirmBody['disposition'])}>
          {bookingOnly ? <option value="booking">保存到已有行程的已订资料</option> : <><option value="candidate">仅作为候选</option><option value="required">确认本次必去</option><option value="excluded">本次排除</option><option value="fixed">确认固定预约时段</option></>}
        </select></label>
        {(['fixed', 'booking'].includes(disposition)) && <>
          <label>开始当地日期<input type="date" value={edit.localDate} onChange={e => change('localDate', e.target.value)} /></label>
          <label>结束当地日期<input type="date" value={edit.endDate} onChange={e => change('endDate', e.target.value)} /></label>
          <label>开始当地时间<input type="time" value={edit.startTime} onChange={e => change('startTime', e.target.value)} /></label>
          <label>结束当地时间<input type="time" value={edit.endTime} onChange={e => change('endTime', e.target.value)} /></label>
          <label>IANA 当地时区<input value={edit.timeZone} placeholder="Europe/Paris" onChange={e => change('timeZone', e.target.value)} /></label>
          <small>日期和时区缺失时不会自动补齐。跨日车票可保存资料，单城求解暂不支持跨午夜固定段。</small>
        </>}
        <label>原币金额（可选）<input type="number" min="0" step="0.01" value={edit.amount} onChange={e => change('amount', e.target.value)} /></label>
        <label>原币代码<input maxLength={3} value={edit.currency} onChange={e => change('currency', e.target.value.toUpperCase())} /></label>
        {(entry.polarity === 'unclear' || (entry.polarity === 'excluded' && disposition !== 'excluded')) && <label><input type="checkbox" checked={overridePolarity} onChange={e => setOverride(e.target.checked)} />我明确确认当前采用方式，覆盖原图否定或不明确的语义</label>}
        <button type="button" className="button button-primary" disabled={!entityId || !requirements.constraintPlanning || (needsPolarityOverride && !overridePolarity)} onClick={() => void adopt()}>{bookingOnly ? '确认并保存已订资料' : '确认加入需求表单'}</button>
      </fieldset>
    </>}
    {notice && <p role="status">{notice}</p>}
  </details>
}
