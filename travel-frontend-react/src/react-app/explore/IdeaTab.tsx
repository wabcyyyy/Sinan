import { useMemo, useState } from 'react'
import { navigate, useLocation } from '../router'
import { inspirationTemplates } from '../data'
import type { InspirationTemplate } from '../data'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'
import { exploreThemes, readExploreTheme } from './ExplorePage'

/**
 * 探索页「旅行灵感」tab：原 InspirationPage 主体原样迁移（2026-10-02 三合一），主题筛选与模板卡不动；深链仍走 /?template=&city=&days=&intent=。
 * 主题进 URL（2026-10-03 PLAN §2.5 F9）：初始值读 ?theme=，切换写 /explore?tab=inspiration&theme=<主题>，深链刷新保持。
 */
export function IdeaTab() {
  const location = useLocation()
  const [theme, setTheme] = useState(() => readExploreTheme(location.query))
  const filtered = useMemo(() => inspirationTemplates.filter((item) => theme === '全部' || item.theme === theme), [theme])
  const pickTheme = (next: string) => { setTheme(next); navigate(`/explore?tab=inspiration&theme=${encodeURIComponent(next)}`) }
  const fill = (item: InspirationTemplate) => navigate(`/?template=${encodeURIComponent(item.id)}&city=${encodeURIComponent(item.city)}&days=${item.days}&intent=${encodeURIComponent(item.intent)}`)
  return <>
    <div className="theme-tabs" role="tablist" aria-label="旅行主题">{exploreThemes.map((item) => <button key={item} role="tab" aria-selected={theme === item} className={theme === item ? 'theme-tab active' : 'theme-tab'} type="button" onClick={() => pickTheme(item)}>{item}</button>)}</div>
    <div className="inspiration-grid">{filtered.map((item, index) => <article className={index === 0 ? 'inspiration-card featured' : 'inspiration-card'} key={item.id}><button className="inspiration-image" type="button" onClick={() => fill(item)}><SmartImg src={item.image} alt={`${item.city}旅行灵感`} ratio="4 / 3" /><span className="inspiration-theme">{item.theme}</span></button><div className="inspiration-copy"><span className="card-kicker">{item.city} · {item.days} 天</span><h2>{item.title}</h2><p>{item.description}</p><div className="inspiration-tags">{item.tags.map((tag) => <span key={tag}>{tag}</span>)}</div><button className="text-action" type="button" onClick={() => fill(item)}>用这个模板开始 <Icon name="arrow" size={15} /></button></div></article>)}</div>
  </>
}
