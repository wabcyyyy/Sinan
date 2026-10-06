import { useMemo, useState } from 'react'
import { guides } from '../data'
import { navigate } from '../router'
import { Icon } from '../shared/Icon'
import { OfflineBadge } from '../shared/States'
import { SmartImg } from '../shared/SmartImg'

/** 探索页「旅行攻略」tab：原 GuidesPage 主体原样迁移（2026-10-02 三合一）；原页头的离线示例徽标收进 tab 内，详情迁 /explore/guide/:slug。 */
export function GuidesTab() {
  const [tag, setTag] = useState('全部')
  const tags = ['全部', ...Array.from(new Set(guides.map((guide) => guide.tag)))]
  const filtered = useMemo(() => guides.filter((guide) => tag === '全部' || guide.tag === tag), [tag])
  return <>
    <div className="guide-offline"><OfflineBadge /></div>
    <div className="guide-filter">{tags.map((item) => <button key={item} className={tag === item ? 'filter-tab active' : 'filter-tab'} type="button" onClick={() => setTag(item)}>{item}</button>)}</div>
    <div className="guide-grid">{filtered.map((guide) => <article className="guide-card" key={guide.slug}><button type="button" className="guide-image" onClick={() => navigate(`/explore/guide/${guide.slug}`)}><SmartImg src={guide.image} alt={`${guide.city}攻略`} ratio="3 / 2" /><span>{guide.tag}</span></button><div className="guide-copy"><span className="card-kicker">{guide.city} · {guide.readTime}</span><h2>{guide.title}</h2><p>{guide.excerpt}</p><button type="button" className="text-action" onClick={() => navigate(`/explore/guide/${guide.slug}`)}>阅读攻略 <Icon name="arrow" size={15} /></button></div></article>)}</div>
  </>
}
