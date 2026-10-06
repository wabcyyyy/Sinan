import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { legacyExploreRedirect } from '../router'
import { destinations } from '../data'
import { DestinationsTab } from './DestinationsTab'
import { ExplorePage, readExploreTab, readExploreTheme } from './ExplorePage'
import { GuidesTab } from './GuidesTab'
import { IdeaTab } from './IdeaTab'

/**
 * 探索页三合一（2026-10-02，PLAN §4/§1.1）的静态渲染测试：同 TopNav.test.ts 纪律——
 * 零新依赖、renderToStaticMarkup 断言结构。tab/theme 解析与旧路径重定向是纯函数，表驱动钉死
 * 「?tab=/?theme= 可直达、旧路径不留死链」的验收口径；交互（点击切 tab）由 navigate 的
 * pushState + popstate 承担，本仓测试基建不做交互测试。
 */

describe('探索页 ?tab= 解析（缺省/非法值回默认 destinations）', () => {
  it.each([
    ['（无参数）', new URLSearchParams(), 'destinations'],
    ['destinations', new URLSearchParams('tab=destinations'), 'destinations'],
    ['inspiration', new URLSearchParams('tab=inspiration'), 'inspiration'],
    ['guides', new URLSearchParams('tab=guides'), 'guides'],
    ['非法值', new URLSearchParams('tab=unknown'), 'destinations'],
  ])('%s → %s', (_name, query, expected) => {
    expect(readExploreTab(query)).toBe(expected)
  })
})

describe('探索页 ?theme= 解析（缺省/非法值回「全部」，PLAN §2.5 F9）', () => {
  it.each([
    ['（无参数）', new URLSearchParams(), '全部'],
    ['合法主题', new URLSearchParams('theme=美食'), '美食'],
    ['合法主题「全部」', new URLSearchParams('theme=全部'), '全部'],
    ['URL 编码的合法主题', new URLSearchParams('tab=inspiration&theme=%E8%87%AA%E7%84%B6'), '自然'],
    ['非法值', new URLSearchParams('theme=unknown'), '全部'],
  ])('%s → %s', (_name, query, expected) => {
    expect(readExploreTheme(query)).toBe(expected)
  })
})

describe('探索页数据口径（PLAN §2.5 拍板①）', () => {
  it('目的地 16 城、国内外交错，前 8 城即城市索引 chips', () => {
    expect(destinations).toHaveLength(16)
    expect(destinations.slice(0, 8).map((item) => item.city)).toEqual(['成都', '东京', '杭州', '巴黎', '上海', '巴厘岛', '北京', '曼谷'])
  })
})

describe('旧探索三页路径 replace 重定向（PLAN §1.1：不留死链）', () => {
  it.each([
    ['/destinations', '/explore?tab=destinations'],
    ['/inspiration', '/explore?tab=inspiration'],
    ['/guides', '/explore?tab=guides'],
    ['/guides/', '/explore?tab=guides'],
    ['/guides/hangzhou-by-the-lake', '/explore/guide/hangzhou-by-the-lake'],
  ])('%s → %s', (from, to) => {
    expect(legacyExploreRedirect(from)).toBe(to)
  })
  it('非旧路径不重定向', () => {
    expect(legacyExploreRedirect('/')).toBeNull()
    expect(legacyExploreRedirect('/explore')).toBeNull()
    expect(legacyExploreRedirect('/explore/guide/chengdu-slow-city')).toBeNull()
    expect(legacyExploreRedirect('/trips')).toBeNull()
  })
})

describe('探索页静态渲染', () => {
  it('页头「先找点方向」+ 默认 destinations tab（搜索框/筛选/网格保留）', () => {
    const html = renderToStaticMarkup(createElement(ExplorePage))
    expect(html).toContain('先找点方向')
    expect(html).toContain('探索分区')
    expect(html).toContain('搜索城市')
    expect(html).toContain('选一座城，换一种日常')
    expect(html).not.toContain('旅行主题')
  })
  it('三个 tab 主体各自可渲染（原三页内容迁移无缺）', () => {
    expect(renderToStaticMarkup(createElement(DestinationsTab))).toContain('搜索城市或旅行方式')
    expect(renderToStaticMarkup(createElement(IdeaTab))).toContain('旅行主题')
    expect(renderToStaticMarkup(createElement(GuidesTab))).toContain('阅读攻略')
    expect(renderToStaticMarkup(createElement(GuidesTab))).toContain('离线示例')
  })
})
