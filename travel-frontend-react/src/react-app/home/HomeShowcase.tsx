import { useRef } from 'react'
import { destinations, inspirationTemplates } from '../data'
import type { InspirationTemplate } from '../data'
import { navigate } from '../router'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'

/** 热门城市横排：destinations 前 6。数据源按国内外交错排列（PLAN 2026-10-03 §2.5），
 * 切片即自然混排；卡片点击走 /?city= 深链（seedFromQuery 预填首句，不自动发送）。
 * 2026-10-03 视觉迭代：3/2 大图优先、城名压图（墨色 scrim 渐变），描述两行定高。 */
export const SHOWCASE_CITIES = destinations.slice(0, 6)

/** 保留原有四张灵感卡片，绑定与城市、天数及主题一致的完整编辑规划。 */
export const SHOWCASE_TEMPLATES = inspirationTemplates.slice(-4)

/** 城市深链拼串。导出仅供测试（深链格式是探索页/首页共用的入口契约）。 */
export function showcaseCityHref(city: string): string {
  return `/?city=${encodeURIComponent(city)}`
}

/** 首页灵感直接查看已编写的路线，不再将文案预填成生成请求。 */
export function showcaseTemplateHref(item: Pick<InspirationTemplate, 'id' | 'itineraryId'>): string {
  if (item.itineraryId == null) throw new Error(`灵感 ${item.id} 尚未绑定完整行程`)
  return `/itineraries/${item.itineraryId}`
}

/** 首页保留热门城市与灵感卡片；灵感扩充为完整路线，支持查看后复制微调。 */
export function HomeShowcase() {
  const citiesRef = useRef<HTMLDivElement>(null)


  const scrollContainer = (ref: { current: HTMLDivElement | null }, offset: number) => {
    ref.current?.scrollBy({ left: offset, behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
  }

  return (
    <section className="home-showcase" aria-label="热门目的地与灵感">
      <div className="showcase-row">
        <div className="showcase-row-head">
          <div><h2>热门城市</h2><p>选一座城，把想法交给司南</p></div>
          <div className="showcase-row-actions">
            <div className="showcase-scroll-controls" aria-label="城市滚动翻页">
              <button
                type="button"
                className="showcase-ctrl-btn"
                aria-label="向前滚动城市"
                title="向左滚动"
                onClick={() => scrollContainer(citiesRef, -320)}
              >
                <Icon name="arrow" size={13} className="is-prev-icon" />
              </button>
              <button
                type="button"
                className="showcase-ctrl-btn"
                aria-label="向后滚动城市"
                title="向右滚动"
                onClick={() => scrollContainer(citiesRef, 320)}
              >
                <Icon name="arrow" size={13} />
              </button>
            </div>
            <button className="text-action" type="button" onClick={() => navigate('/explore?tab=destinations')}>全部目的地<Icon name="arrow" size={15} /></button>
          </div>
        </div>
        <div className="showcase-scroll-container">
          <div className="showcase-cities" ref={citiesRef}>
            {SHOWCASE_CITIES.map((item) => (
              <button key={item.city} className="showcase-city" type="button"
                onClick={() => navigate(showcaseCityHref(item.city))}>
                <span className="showcase-city-media">
                  <SmartImg src={item.image} alt={`${item.city}旅行封面`} ratio="3 / 2" />
                  <span className="showcase-city-scrim" aria-hidden="true" />
                  <span className="showcase-city-name">{item.city}<small>{item.province}</small></span>
                </span>
                <span className="showcase-city-copy">{item.description}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="showcase-row">
        <div className="showcase-row-head">
          <div><h2>灵感起点</h2><p>看看完整路线，复制一份，再按自己的节奏微调</p></div>
          <div className="showcase-row-actions">
            <button className="text-action" type="button" onClick={() => navigate('/explore?tab=inspiration')}>更多灵感<Icon name="arrow" size={15} /></button>
          </div>
        </div>
        <div className="showcase-scroll-container">
          <div className="showcase-ideas">
            {SHOWCASE_TEMPLATES.map((item) => (
              <button key={item.id} className="showcase-idea" type="button"
                onClick={() => navigate(showcaseTemplateHref(item))}>
                <SmartImg src={item.image} alt={`${item.city}灵感封面`} />
                <span className="showcase-idea-scrim" aria-hidden="true" />
                <span className="showcase-idea-theme">{item.theme}</span>
                <span className="showcase-idea-body">
                  <span className="card-kicker">{item.city} · {item.days} 天</span>
                  <strong>{item.title}</strong>
                  <span className="showcase-idea-desc">{item.description}</span>
                  <span className="showcase-idea-cta">查看完整行程<Icon name="arrow" size={13} /></span>
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}
