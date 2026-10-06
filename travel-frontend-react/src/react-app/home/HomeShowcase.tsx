import { useEffect, useRef } from 'react'
import { destinations, inspirationTemplates } from '../data'
import type { InspirationTemplate } from '../data'
import { navigate } from '../router'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'

/** 热门城市横排：destinations 前 6。数据源按国内外交错排列（PLAN 2026-10-03 §2.5），
 * 切片即自然混排；卡片点击走 /?city= 深链（seedFromQuery 预填首句，不自动发送）。
 * 2026-10-03 视觉迭代：3/2 大图优先、城名压图（墨色 scrim 渐变），描述两行定高。 */
export const SHOWCASE_CITIES = destinations.slice(0, 6)

/** 灵感起点小卡行：inspirationTemplates 后 4 条，避开 greeting 开场 chips 已占用的
 * 前 4 条（ChatIntake GREETING_TEMPLATES = slice(0, 4)），点击走与 IdeaTab.fill
 * 同格式的 /?template= 深链。 */
export const SHOWCASE_TEMPLATES = inspirationTemplates.slice(-4)

/** 城市深链拼串。导出仅供测试（深链格式是探索页/首页共用的入口契约）。 */
export function showcaseCityHref(city: string): string {
  return `/?city=${encodeURIComponent(city)}`
}

/** 模板深链拼串：与 IdeaTab.fill 逐段同构（/?template=&city=&days=&intent=）。
 * 导出仅供测试。 */
export function showcaseTemplateHref(item: Pick<InspirationTemplate, 'id' | 'city' | 'days' | 'intent'>): string {
  return `/?template=${encodeURIComponent(item.id)}&city=${encodeURIComponent(item.city)}&days=${item.days}&intent=${encodeURIComponent(item.intent)}`
}

/** 自动平滑滚动挂钩：定时按步进自动推进，悬停或触控交互时暂停，循环无缝回弹 */
function useAutoScroll(ref: { current: HTMLDivElement | null }, step: number, intervalMs: number) {
  useEffect(() => {
    const el = ref.current
    if (!el || typeof window === 'undefined') return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) return

    let paused = false
    const onEnter = () => { paused = true }
    const onLeave = () => { paused = false }

    el.addEventListener('mouseenter', onEnter)
    el.addEventListener('mouseleave', onLeave)
    el.addEventListener('touchstart', onEnter, { passive: true })
    el.addEventListener('touchend', onLeave, { passive: true })

    const timer = setInterval(() => {
      if (paused || !ref.current) return
      const node = ref.current
      const maxScroll = node.scrollWidth - node.clientWidth
      if (maxScroll <= 10) return
      if (node.scrollLeft >= maxScroll - 20) {
        node.scrollTo({ left: 0, behavior: 'smooth' })
      } else {
        node.scrollBy({ left: step, behavior: 'smooth' })
      }
    }, intervalMs)

    return () => {
      clearInterval(timer)
      el.removeEventListener('mouseenter', onEnter)
      el.removeEventListener('mouseleave', onLeave)
      el.removeEventListener('touchstart', onEnter)
      el.removeEventListener('touchend', onLeave)
    }
  }, [ref, step, intervalMs])
}

/** idle 态对话卡下方的轻展示区（PLAN 2026-10-03 §2.2 + 同日视觉迭代）：两行真横向
 * 自动滚动陈列（大卡溢出产生翻页感，scroll-snap 对齐，悬停暂停），行头右侧给分区入口与翻页微控；
 * 全部复用静态 data.ts 与现有令牌，图片走 SmartImg lazy，零重依赖。 */
export function HomeShowcase() {
  const citiesRef = useRef<HTMLDivElement>(null)
  const ideasRef = useRef<HTMLDivElement>(null)

  useAutoScroll(citiesRef, 280, 3800)
  useAutoScroll(ideasRef, 300, 4400)

  const scrollContainer = (ref: { current: HTMLDivElement | null }, offset: number) => {
    ref.current?.scrollBy({ left: offset, behavior: 'smooth' })
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
          <div><h2>灵感起点</h2><p>套一条现成的说法，开聊只要一句话</p></div>
          <div className="showcase-row-actions">
            <div className="showcase-scroll-controls" aria-label="灵感滚动翻页">
              <button
                type="button"
                className="showcase-ctrl-btn"
                aria-label="向前滚动灵感"
                title="向左滚动"
                onClick={() => scrollContainer(ideasRef, -320)}
              >
                <Icon name="arrow" size={13} className="is-prev-icon" />
              </button>
              <button
                type="button"
                className="showcase-ctrl-btn"
                aria-label="向后滚动灵感"
                title="向右滚动"
                onClick={() => scrollContainer(ideasRef, 320)}
              >
                <Icon name="arrow" size={13} />
              </button>
            </div>
            <button className="text-action" type="button" onClick={() => navigate('/explore?tab=inspiration')}>更多灵感<Icon name="arrow" size={15} /></button>
          </div>
        </div>
        <div className="showcase-scroll-container">
          <div className="showcase-ideas" ref={ideasRef}>
            {SHOWCASE_TEMPLATES.map((item) => (
              <button key={item.id} className="showcase-idea" type="button"
                onClick={() => navigate(showcaseTemplateHref(item))}>
                <span className="card-kicker">{item.city} · {item.days} 天</span>
                <strong>{item.title}</strong>
                <span>{item.description}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}
