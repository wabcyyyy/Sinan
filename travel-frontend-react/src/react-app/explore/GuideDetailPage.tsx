import { guides } from '../data'
import { navigate, routeId } from '../router'
import { Icon } from '../shared/Icon'
import { OfflineBadge } from '../shared/States'
import { SmartImg } from '../shared/SmartImg'

/** 攻略详情：原 guides/GuidesPages.tsx 的 GuideDetailPage 原样迁移（2026-10-02 三合一，路由 /explore/guide/:slug），展示逻辑不动。 */
export function GuideDetailPage({ path }: { path: string }) {
  const slug = routeId(path, '/explore/guide/')
  const guide = guides.find((item) => item.slug === slug)
  if (!guide) return <div className="page-content"><div className="inline-empty"><strong>攻略不存在</strong><span>返回攻略列表，换一座城市继续阅读。</span><button className="button button-secondary" type="button" onClick={() => navigate('/explore?tab=guides')}>回到攻略</button></div></div>
  return <div className="page-content guide-detail"><button className="back-link" type="button" onClick={() => navigate('/explore?tab=guides')}><Icon name="arrow" size={16} />返回攻略列表</button><div className="guide-detail-hero"><SmartImg src={guide.image} ratio="3 / 2" eager alt={`${guide.city}攻略`} /><div><OfflineBadge /><span className="section-eyebrow">{guide.city} · {guide.readTime}</span><h1>{guide.title}</h1><p>{guide.excerpt}</p><button className="button button-primary" type="button" onClick={() => navigate(`/?city=${encodeURIComponent(guide.city)}`)}>按这座城市规划<Icon name="arrow" size={17} /></button></div></div><article className="guide-article"><p className="article-note">示例内容 · 城市事实、营业时间和预约规则请在出发前核实。</p>{guide.sections.map((section) => <section key={section.title}><h2>{section.title}</h2><p>{section.body}</p></section>)}</article></div>
}
