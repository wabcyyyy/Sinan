import { useEffect, useState } from 'react'
import type { TemplateCardVO, TemplateDetailVO } from '../../types/generated/contracts'
import { getPublicItinerary, listPublicItineraries } from '../../api/sinan'
import { destinations } from '../data'
import { navigate } from '../router'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'
import { EmptyBlock, ErrorBlock, LoadingBlock } from '../shared/States'
import { PublicRouteContent } from './PublicRouteContent'
import { CopyItineraryButton } from './CopyItineraryButton'
import './templates.css'

export function PublicItineraries() {
  const [rows, setRows] = useState<TemplateCardVO[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let disposed = false
    setLoading(true)
    setError('')
    listPublicItineraries().then((items) => { if (!disposed) setRows(items) })
      .catch((err) => { if (!disposed) setError(err instanceof Error ? err.message : '行程库暂时不可用') })
      .finally(() => { if (!disposed) setLoading(false) })
    return () => { disposed = true }
  }, [retry])
  const filtered = rows.filter((row) => `${row.city} ${row.title} ${row.intro}`.includes(query.trim()))
  return <section className="page-content public-itineraries" aria-label="公开行程库">
    <header className="public-heading"><div><span className="section-eyebrow">从一条路线开始</span><h1>不必每次，从空白出发。</h1><p>从湖岸、街巷到一顿好饭。先读懂路线，再复制成自己的旅行。</p></div></header>
    <label className="public-search">寻找一条路线<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索城市、路线或主题" type="search" /></label>
    {loading ? <LoadingBlock label="正在打开行程库…" /> : error ? <ErrorBlock message={error} onRetry={() => setRetry((value) => value + 1)} /> : !filtered.length ? <EmptyBlock title={query ? '没有找到匹配的路线' : '还没有公开路线'} description="可以换个关键词，或从自己的行程中发布一份规划。" /> : <div className="public-route-grid">{filtered.map((row) => <RouteCard key={row.id} row={row} />)}</div>}
    <p className="public-footnote">“编辑规划”是可复用的路线参考；“用户公开”由用户主动发布。两者都不代表已实地完成出行。</p>
  </section>
}

function RouteCard({ row }: { row: TemplateCardVO }) {
  const cover = row.coverUrl || destinations.find((item) => item.city === row.city)?.image
  return <button type="button" className="public-route-card" onClick={() => navigate(`/itineraries/${row.id}`)}>
    {cover ? <SmartImg src={cover} alt={row.coverUrl ? `${row.city}公开规划封面` : `${row.city}城市氛围图`} /> : <div className="public-route-placeholder">{row.city}</div>}
    <span className="public-route-copy"><span className="public-route-meta">{row.sourceType === 'editorial' ? '司南编辑规划' : '用户公开规划'} · {row.days} 天</span><strong>{row.title}</strong><span>{row.intro || '打开逐日路线，看看这座城市的另一种安排。'}</span><b>查看路线<Icon name="arrow" size={15} /></b></span>
  </button>
}

export function PublicItineraryDetail({ id, signedIn }: { id: number; signedIn: boolean }) {
  const [data, setData] = useState<TemplateDetailVO | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let disposed = false
    setData(null)
    setError('')
    getPublicItinerary(id).then((row) => { if (!disposed) setData(row) })
      .catch((err) => { if (!disposed) setError(err instanceof Error ? err.message : '这份行程已下架或暂时无法打开') })
    return () => { disposed = true }
  }, [id, retry])
  if (error) return <div className="page-content"><ErrorBlock message={error} onRetry={() => setRetry((value) => value + 1)} /><button className="button button-secondary" onClick={() => navigate('/itineraries')}>回到行程库</button></div>
  if (!data) return <LoadingBlock label="正在展开这条路线…" />
  const { summary } = data
  const cover = data.coverUrl || destinations.find((item) => item.city === data.city)?.image
  return <article className="page-content public-detail">
    <button type="button" className="back-link" onClick={() => navigate('/itineraries')}><Icon name="arrow" size={16} />行程库</button>
    <header className="public-detail-hero" id="route-overview"><div><span className="section-eyebrow">{data.sourceType === 'editorial' ? '司南编辑规划' : '用户主动公开的规划'} · {data.city}</span><h1>{data.title}</h1><p>{summary.intro}</p><div className="public-route-facts"><span>{summary.days} 天</span><span>{summary.persons} 人参考</span><span>{summary.dayList.reduce((count, day) => count + day.items.length, 0)} 个安排</span></div><CopyItineraryButton templateId={id} signedIn={signedIn} /><small>从这条路线出发，日期与节奏由你决定。</small></div>{cover && <SmartImg src={cover} alt={data.coverUrl ? `${data.city}公开规划封面` : `${data.city}城市氛围图`} eager />}</header>
    <PublicRouteContent summary={summary} />
  </article>
}
