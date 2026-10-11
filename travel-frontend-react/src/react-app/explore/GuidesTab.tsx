import { useMemo, useState } from 'react'
import { guides } from '../data'
import { navigate } from '../router'
import { Icon } from '../shared/Icon'
import { OfflineBadge } from '../shared/States'
import { SmartImg } from '../shared/SmartImg'

/** 探索页「旅行攻略」tab：原 GuidesPage 主体原样迁移（2026-10-02 三合一）；原页头的离线示例徽标收进 tab 内，详情迁 /explore/guide/:slug。
 * 离线示例徽标保留（攻略是随包静态内容），旁边补一句面向用户的含义，不让它孤零零像开发标记。 */
export function GuidesTab() {
  const [tag, setTag] = useState('全部')
  const tags = ['全部', ...Array.from(new Set(guides.map((guide) => guide.tag)))]
  const filtered = useMemo(() => guides.filter((guide) => tag === '全部' || guide.tag === tag), [tag])
  return <>
    <p className="guide-offline"><OfflineBadge />这些攻略随应用附带、不会实时更新，出发前请再核实营业时间与交通。</p>
    <div className="guide-filter">{tags.map((item) => <button key={item} className={tag === item ? 'filter-tab active' : 'filter-tab'} type="button" onClick={() => setTag(item)}>{item}</button>)}</div>
    <div className="guide-grid">{filtered.map((guide) => <article className="guide-card" key={guide.slug}><button type="button" className="guide-image" onClick={() => navigate(`/explore/guide/${guide.slug}`)}><SmartImg src={guide.image} alt={`${guide.city}攻略`} ratio="3 / 2" /><span className="guide-tag">{guide.tag}</span></button><div className="guide-copy"><span className="card-kicker">{guide.city} · {guide.readTime}</span><h2>{guide.title}</h2><p>{guide.excerpt}</p><button type="button" className="text-action" onClick={() => navigate(`/explore/guide/${guide.slug}`)}>阅读攻略 <Icon name="arrow" size={15} /></button></div></article>)}</div>
  </>
}
