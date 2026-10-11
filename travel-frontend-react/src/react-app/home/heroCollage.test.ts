import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { destinations } from '../data'
import { HERO_COLLAGE, HeroCollage } from './HeroCollage'
import { SHOWCASE_CITIES, SHOWCASE_TEMPLATES } from './HomeShowcase'
import { HomeStudio } from './HomeStudio'

/**
 * 首屏照片拼贴（视觉体检 2026-10-11）：图源只能来自 destinations（已在 CREDITS.md 登记署名），
 * 纯装饰不进可访问树；固定示例入口只给未登录访客。
 */

describe('HeroCollage', () => {
  it('6 张全部命中 destinations，不引入未登记的图', () => {
    expect(HERO_COLLAGE).toHaveLength(6)
    for (const item of HERO_COLLAGE) expect(destinations).toContain(item)
  })

  it('与下方展示区（热门城市 + 灵感起点）不重复用图', () => {
    const below = new Set([...SHOWCASE_CITIES.map((item) => item.image), ...SHOWCASE_TEMPLATES.map((item) => item.image)])
    for (const item of HERO_COLLAGE) expect(below.has(item.image)).toBe(false)
  })

  it('整块 aria-hidden，图片 alt 为空', () => {
    const html = renderToStaticMarkup(createElement(HeroCollage))
    expect(html).toContain('class="hero-collage" aria-hidden="true"')
    expect(html).not.toMatch(/alt="[^"]+"/)
  })
})

describe('HomeStudio 固定示例入口', () => {
  it('未登录访客在首屏看到回放入口，已登录不显示', () => {
    const query = new URLSearchParams()
    expect(renderToStaticMarkup(createElement(HomeStudio, { query, showDemoLink: true }))).toContain('href="/demo"')
    expect(renderToStaticMarkup(createElement(HomeStudio, { query }))).not.toContain('href="/demo"')
  })
})
