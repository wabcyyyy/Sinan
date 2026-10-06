import { useMemo, useState } from 'react'
import { navigate } from '../router'
import { destinations, type DestinationCategory } from '../data'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'

const categories: DestinationCategory[] = ['全部', '人文', '美食', '自然', '慢旅行']

/**
 * 探索页「目的地」tab：原 DestinationsPage 主体原样迁移（2026-10-02 三合一），搜索/分类筛选/卡片网格不动。
 * 城市索引 chips 对齐 destinations.slice(0, 8)（2026-10-03 PLAN §2.5 F8）：AI-native 数据面下任何城市可规划，
 * 旧城市字典只剩误导，getSupportedCities 依赖随之摘除（后端端点本身保留不动）。
 */
export function DestinationsTab() {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<DestinationCategory>('全部')
  const filtered = useMemo(() => destinations.filter((item) => {
    const matchQuery = !query.trim() || `${item.city}${item.province}${item.description}`.includes(query.trim())
    const matchCategory = category === '全部' || item.categories.includes(category)
    return matchQuery && matchCategory
  }), [category, query])
  return <>
    <section className="destination-toolbox" aria-label="目的地筛选"><label className="search-field"><Icon name="search" size={19} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索城市或旅行方式" aria-label="搜索城市" />{query && <button type="button" aria-label="清除搜索" onClick={() => setQuery('')}><Icon name="close" size={16} /></button>}</label><div className="filter-tabs">{categories.map((item) => <button key={item} className={category === item ? 'filter-tab active' : 'filter-tab'} type="button" onClick={() => setCategory(item)}>{item}</button>)}</div></section>
    <section className="destination-feature"><div><span className="section-eyebrow">城市索引</span><h2>从熟悉的地方开始，<br />也可以去一点远方。</h2><p>司南会参考城市的节奏、交通和当下可获得的信息。没有答案时，也会清楚地告诉你这是一个草案。</p><div className="city-pills">{destinations.slice(0, 8).map((item) => <button key={item.city} type="button" onClick={() => navigate(`/?city=${encodeURIComponent(item.city)}`)}><Icon name="pin" size={15} />{item.city}</button>)}</div></div><SmartImg src={destinations[1].image} alt="东京新宿的天际线与远处的富士山" ratio="3 / 2" eager /></section>
    <div className="section-row"><div><span className="section-eyebrow">热门城市</span><h2>选一座城，换一种日常</h2></div><span className="result-count">{filtered.length} 个目的地</span></div>
    {filtered.length ? <div className="destination-grid">{filtered.map((item) => <article className="destination-card" key={item.city}><button className="destination-image" type="button" onClick={() => navigate(`/?city=${encodeURIComponent(item.city)}`)}><SmartImg src={item.image} alt={`${item.city}城市参考`} ratio="4 / 3" /><span className="image-arrow"><Icon name="arrowUpRight" size={17} /></span></button><div className="destination-card-body"><div className="card-kicker"><span>{item.province}</span><span>{item.days}</span></div><h3>{item.city}</h3><p>{item.description}</p><div className="card-footer"><span>{item.bestFor}</span><button type="button" onClick={() => navigate(`/?city=${encodeURIComponent(item.city)}`)}>开始规划 <Icon name="arrow" size={15} /></button></div></div></article>)}</div> : <div className="inline-empty"><Icon name="search" size={22} /><strong>没有找到匹配的城市</strong><span>换个关键词，或从热门城市开始。</span></div>}
  </>
}
