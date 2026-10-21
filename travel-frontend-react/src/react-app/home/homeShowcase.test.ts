import { activateIntakeOwner, intakeSessionStorage } from './intakeStorage'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, afterEach, describe, expect, it } from 'vitest'
import { destinations, inspirationTemplates } from '../data'
import { CURATED_ITINERARIES } from '../../types/generated/contracts'
import { HomeShowcase, SHOWCASE_CITIES, SHOWCASE_TEMPLATES, showcaseCityHref, showcaseTemplateHref } from './HomeShowcase'
import { HomeStudio } from './HomeStudio'
import { saveIntake } from './intakeSlots'

/** 保留原有灵感卡片及样式，将入口绑定为完整路线，不增加第三排。 */

describe('HomeShowcase（切片与静态冒烟）', () => {
  it('热门城市 = destinations 前 6；灵感起点 = inspirationTemplates 后 4', () => {
    expect(SHOWCASE_CITIES).toEqual(destinations.slice(0, 6))
    expect(SHOWCASE_TEMPLATES).toEqual(inspirationTemplates.slice(-4))
  })

  it('保留城市与灵感两行；灵感沿用原样式并提供完整行程入口', () => {
    const html = renderToStaticMarkup(createElement(HomeShowcase))
    expect(html).toContain('home-showcase')
    expect(html).toContain('showcase-cities')
    expect(html).toContain('showcase-ideas')
    expect(html).toContain('热门城市')
    expect(html).toContain('灵感起点')
    expect(html).toContain('全部目的地')
    expect(html).toContain('更多灵感')
    expect(html).toContain('查看完整行程')
    expect(html).toContain('4 / 5')
    expect(html).not.toContain('用这句开场')
    expect(html).not.toContain('public-itineraries')
    expect(html).not.toContain('行程分享')
    expect(html).toContain('loading="lazy"')
    expect(html).toContain('3 / 2')
  })

  it('切片条目逐一上屏：城市卡带城名，灵感卡带标题', () => {
    const html = renderToStaticMarkup(createElement(HomeShowcase))
    for (const city of SHOWCASE_CITIES) expect(html).toContain(city.city)
    for (const item of SHOWCASE_TEMPLATES) expect(html).toContain(item.title)
  })
})

describe('showcase 入口（城市需求预填与完整灵感行程）', () => {
  it('城市卡 /?city=<城>：与 DestinationsTab 城市 pills 深链同格式', () => {
    expect(showcaseCityHref('成都')).toBe('/?city=%E6%88%90%E9%83%BD')
  })

  it('原有四张灵感均绑定到同标题、城市和天数的完整规划', () => {
    expect(SHOWCASE_TEMPLATES).toHaveLength(4)
    for (const item of SHOWCASE_TEMPLATES) {
      const route = CURATED_ITINERARIES.find((row) => row.id === item.itineraryId)!
      expect(route).toBeDefined()
      expect([route.title, route.city, route.days]).toEqual([item.title, item.city, item.days])
      expect(route.summary.dayList).toHaveLength(item.days)
      expect(route.summary.dayList.every((day) => day.items.length >= 3)).toBe(true)
      expect(showcaseTemplateHref(item)).toBe(`/itineraries/${route.id}`)
    }
  })
})

describe('HomeStudio × HomeShowcase（idle-only 挂载）', () => {
  afterEach(() => intakeSessionStorage.removeItem('current'))

  it('idle：展示区跟在一屏余量之后上屏', () => {
    const html = renderToStaticMarkup(createElement(HomeStudio, { query: new URLSearchParams() }))
    expect(html).toContain('home-studio is-idle')
    expect(html).toContain('home-showcase')
  })

  it('active（有会话历史恢复）：双栏工作台不渲染展示区', () => {
    // M2 存储带版本号：走 saveIntake 统一写入（手写 JSON 缺 v 字段会被 loadIntake 拒收）
    saveIntake({
      messages: [{ id: 'greeting', role: 'assistant', text: 'hi' }, { id: 'u1', role: 'user', text: '想去成都' }],
      slots: { city: '成都' },
      firstMessage: '想去成都',
    })
    const html = renderToStaticMarkup(createElement(HomeStudio, { query: new URLSearchParams() }))
    expect(html).toContain('home-studio is-active')
    expect(html).toContain('home-studio-panel')
    expect(html).not.toContain('home-showcase')
  })
})

beforeEach(() => { activateIntakeOwner(null) })
