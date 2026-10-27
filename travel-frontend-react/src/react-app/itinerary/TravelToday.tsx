import type { ItineraryDetail } from '../../types/itinerary'
import type { PreparationController } from './usePreparation'
import { directionsLink } from './mapPins'

export function destinationDate(timeZone = 'Asia/Shanghai', instant = new Date()) {
  return new Intl.DateTimeFormat('sv-SE', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(instant)
}

export function TravelToday({ trip, dayNo, setDayNo, preparation, canMark, onMap }: {
  trip: ItineraryDetail; dayNo: number; setDayNo: (day: number) => void; preparation: PreparationController; canMark: boolean; onMap: () => void
}) {
  const day = trip.dayList.find(d => d.dayNo === dayNo) || trip.dayList[0]
  const timeZone = day.constraintPlanning?.destination.timeZone || 'Asia/Shanghai'
  const today = destinationDate(timeZone)
  const done = preparation.data.completedItemIds
  const currentTime = new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date())
  const nextIndex = day.items.findIndex(p => p.id && !done.includes(p.id) && (day.travelDate !== today || !p.endTime || p.endTime.slice(0, 5) >= currentTime))
  const next = nextIndex >= 0 ? day.items[nextIndex] : null
  const prev = nextIndex > 0 ? day.items[nextIndex - 1] : undefined
  const linked = preparation.data.bookings.filter(b => b.itemId === next?.id)
  const mark = (id: number) => void preparation.save({ ...preparation.data, completedItemIds: done.includes(id) ? done.filter(x => x !== id) : [...done, id] })
  return <section className="travel-today" aria-label="出行模式">
    <header><span className="section-eyebrow">出行模式 · 当前时间按目的地 {timeZone}</span><h2>{day.travelDate === today ? '今天' : `第 ${day.dayNo} 天`}{day.theme && ` · ${day.theme}`}</h2>
      {day.travelDate !== today && <p>行程日期 {day.travelDate || '未设置'}。这里展示第 {day.dayNo} 天的准备视图。</p>}</header>
    <nav className="today-days" aria-label="切换出行日期">{trip.dayList.map(d => <button type="button" key={d.dayNo} aria-pressed={d.dayNo === day.dayNo} onClick={() => setDayNo(d.dayNo)}>第 {d.dayNo} 天{d.travelDate === today ? ' · 今天' : ''}</button>)}</nav>
    {next ? <div className="today-next"><small>下一站 · {next.startTime?.slice(0, 5) || '时间待定'}</small><h3>{next.poiName}</h3><p>{next.address || next.remark || '出发前核实地点与交通'}</p>
      <div className="prep-actions"><a className="button button-primary" href={directionsLink(trip.city, next, prev)} target="_blank" rel="noreferrer">{prev ? '打开两点导航' : '导航到这一站'} ↗</a><button type="button" onClick={onMap}>在行程地图中看</button>{canMark && next.id && <button type="button" disabled={!preparation.state || preparation.busy} onClick={() => mark(next.id!)}>标记已到达</button>}</div>
      <p className="prep-caption">外部导航按城市、地点名与地址重新匹配；核对起终点后再出发。这里不提供实时路况。</p>
      {linked.map(b => <details key={b.id} className="today-booking"><summary>已订资料 · {b.name}</summary><p>确认号 {b.reference || '未填写'}</p><p>{b.note}</p></details>)}
    </div> : <p className="prep-empty">当前日期没有未完成的后续安排。可切换日期或查看完整行程。</p>}
    <h3>当天安排</h3><ol className="today-schedule">{day.items.map((item, i) => <li key={item.id || i}><time>{item.startTime?.slice(0, 5) || '--:--'}</time><div><strong>{item.poiName}</strong><small>{item.address || item.remark}</small></div>{canMark && item.id && <label><input type="checkbox" checked={done.includes(item.id)} disabled={!preparation.state || preparation.busy} onChange={() => mark(item.id!)} />已到达</label>}</li>)}</ol>
    <button className="button button-secondary" type="button" onClick={onMap}>查看当前日地图</button>
  </section>
}
