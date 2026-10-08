import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it } from 'vitest'
import { destinations, inspirationTemplates } from '../data'
import { HomeShowcase, SHOWCASE_CITIES, SHOWCASE_TEMPLATES, showcaseCityHref, showcaseTemplateHref } from './HomeShowcase'
import { HomeStudio } from './HomeStudio'
import { saveIntake } from './intakeSlots'

/**
 * 首页 idle 轻展示区（PLAN 2026-10-03 §2.2）静态断言：切片口径、深链拼串格式
 * （与 explore 页 DestinationsTab 城市深链 / IdeaTab.fill 模板深链逐段同构）、
 * HomeStudio 的 idle-only 挂载。renderToStaticMarkup 静态渲染，零新依赖。
 */

describe('HomeShowcase（切片与静态冒烟）', () => {
  it('热门城市 = destinations 前 6；灵感起点 = inspirationTemplates 后 4', () => {
    expect(SHOWCASE_CITIES).toEqual(destinations.slice(0, 6))
    expect(SHOWCASE_TEMPLATES).toEqual(inspirationTemplates.slice(-4))
  })

  it('出热门城市行、灵感行与行头分区入口；封面走 SmartImg lazy 3/2', () => {
    const html = renderToStaticMarkup(createElement(HomeShowcase))
    expect(html).toContain('home-showcase')
    expect(html).toContain('showcase-cities')
    expect(html).toContain('showcase-ideas')
    expect(html).toContain('热门城市')
    expect(html).toContain('灵感起点')
    expect(html).toContain('全部目的地')
    expect(html).toContain('更多灵感')
    expect(html).toContain('loading="lazy"')
    expect(html).toContain('3 / 2')
  })

  it('切片条目逐一上屏：城市卡带城名，灵感卡带标题', () => {
    const html = renderToStaticMarkup(createElement(HomeShowcase))
    for (const city of SHOWCASE_CITIES) expect(html).toContain(city.city)
    for (const item of SHOWCASE_TEMPLATES) expect(html).toContain(item.title)
  })
})

describe('showcase 深链拼串（与 explore 页入口契约同构）', () => {
  it('城市卡 /?city=<城>：与 DestinationsTab 城市 pills 深链同格式', () => {
    expect(showcaseCityHref('成都')).toBe('/?city=%E6%88%90%E9%83%BD')
  })

  it('灵感卡 /?template=&city=&days=&intent=：与 IdeaTab.fill 逐段一致（含编码）', () => {
    expect(showcaseTemplateHref({ id: 'chengdu-slow', city: '成都', days: 3, intent: '带父母去成都 3 天' })).toBe(
      '/?template=chengdu-slow&city=%E6%88%90%E9%83%BD&days=3&intent=' + encodeURIComponent('带父母去成都 3 天'),
    )
    for (const item of SHOWCASE_TEMPLATES) {
      expect(showcaseTemplateHref(item)).toBe(
        `/?template=${encodeURIComponent(item.id)}&city=${encodeURIComponent(item.city)}&days=${item.days}&intent=${encodeURIComponent(item.intent)}`,
      )
    }
  })
})

describe('HomeStudio × HomeShowcase（idle-only 挂载）', () => {
  afterEach(() => sessionStorage.removeItem('sinan-intake-v1'))

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
