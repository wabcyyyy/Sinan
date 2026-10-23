import type { TemplateSummaryVO } from '../../types/generated/contracts'
import { Icon } from '../shared/Icon'

const kinds: Record<string, string> = { attraction: '游览', food: '用餐', rest: '留白', transport: '交通', activity: '体验', hotel: '住宿' }

export function PublicRouteContent({ summary }: { summary: TemplateSummaryVO }) {
  return <>
    <nav className="public-day-nav" aria-label="每日路线速览">
      {summary.dayList.map(day => <a key={day.dayNo} href={`#route-day-${day.dayNo}`}>
        <span>DAY {String(day.dayNo).padStart(2, '0')}<small>{day.items.length} 个安排</small></span>
        <strong>{day.theme || `第 ${day.dayNo} 天`}</strong>
        <span className="public-day-stops">{day.items.filter(item => item.itemType === 'attraction').map(item => item.poiName).join(' → ') || '按自己的节奏出发'}</span>
      </a>)}
    </nav>
    {summary.guide.length > 0 && <section className="public-guide" aria-label="路线指南">
      <div className="public-guide-intro"><span className="section-eyebrow">出发之前</span><h2>先读懂这条路线</h2><p>把选择做在出发前，把余量留在旅途中。</p></div>
      <div className="public-guide-grid">{summary.guide.map((part, index) => <section key={part.title}>
        <span className="public-guide-number">0{index + 1}</span><div><h3>{part.title}</h3><p>{part.text}</p></div>
      </section>)}</div>
    </section>}
    <div className="public-detail-body"><div>{summary.dayList.map(day => <section className="public-day" id={`route-day-${day.dayNo}`} key={day.dayNo}>
      <header><span>DAY {String(day.dayNo).padStart(2, '0')}</span><h2>{day.theme || `第 ${day.dayNo} 天`}</h2>{day.overview && <p className="public-day-overview">{day.overview}</p>}</header>
      <ol>{day.items.map((item, index) => <li key={`${day.dayNo}-${index}`}>
        <time>{item.startTime?.slice(0, 5) || '时间自定'}<span>{item.endTime?.slice(0, 5)}</span></time>
        <div className="public-stop"><span className="public-stop-kind">{String(index + 1).padStart(2, '0')} · {kinds[item.itemType || ''] || '安排'}</span><h3>{item.poiName}</h3>
          {item.tip && <p>{item.tip}</p>}
          {item.address && <div className="public-stop-location"><span>{item.address}</span>{!['rest', 'transport', 'food'].includes(item.itemType || '') && <a href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${summary.city} ${item.poiName} ${item.address}`)}`} target="_blank" rel="noreferrer" aria-label={`在地图中查找${item.poiName}`}><Icon name="pin" size={13} />地图查找<Icon name="arrow" size={12} /></a>}</div>}
        </div>
      </li>)}</ol>
      <a className="public-back-overview" href="#route-overview">回到路线概览 ↑</a>
    </section>)}</div>
      <aside className="public-route-notes"><span className="section-eyebrow">行前手记</span><h2>带着余量出发</h2><p>复制路线后，先填自己的日期，再按到达和返程时间删减安排。</p><h3>这趟需要留意</h3><ul>{(summary.notes.length ? summary.notes : ['开放、预约与交通可能变化，出发前再确认。']).map(note => <li key={note}>{note}</li>)}</ul>
        {summary.references.length > 0 && <><h3>官方信息入口</h3><ul>{summary.references.filter(url => /^https:\/\//.test(url)).map(url => <li key={url}><a href={url} target="_blank" rel="noreferrer">{new URL(url).hostname}<Icon name="arrow" size={12} /></a></li>)}</ul></>}
        {summary.costTiers.length > 0 && <><h3>费用参考</h3>{summary.costTiers.map(tier => <p key={tier.category}>{tier.category} · {tier.amountRangeText}</p>)}</>}
        <details className="public-source-note"><summary>关于这份路线</summary><p>{summary.sourceType === 'editorial' ? '司南编辑规划参考，非实地游记。' : '用户主动公开的规划快照。'}时间为建议停留窗口；开放、价格与交通请以出行当天信息为准。复制不含私人日期、预算、聊天或预订。</p></details>
      </aside>
    </div>
  </>
}
