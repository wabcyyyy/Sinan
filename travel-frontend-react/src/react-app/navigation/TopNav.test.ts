import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { TopNav } from './TopNav'

/**
 * 登出接线（P1-7）的静态渲染测试：同 TripBadges.test.ts 纪律——零新依赖、
 * renderToStaticMarkup 断言结构。点击登出后的 logout 调用 + localStorage 清理 +
 * 跳转属交互行为，本仓测试基建（无 testing-library/act）不做交互测试，接线由
 * App.tsx 内联 handler 承担，这里钉住两种登录态的入口可见性。
 */

describe('TopNav 登录态入口', () => {
  it('已登录：显示用户名与登出入口，不显示登录/注册', () => {
    const html = renderToStaticMarkup(createElement(TopNav, { username: 'wanderer', onLogout: () => {} }))
    expect(html).toContain('wanderer')
    expect(html).toContain('登出')
    expect(html).not.toContain('登录 / 注册')
  })
  it('未登录：保持登录/注册入口，无用户名与登出', () => {
    const html = renderToStaticMarkup(createElement(TopNav, {}))
    expect(html).toContain('登录 / 注册')
    expect(html).not.toContain('登出')
  })
})

describe('TopNav 导航三项（2026-10-03 补「首页」，PLAN §2.1；三合一旧断言保留）', () => {
  it('主导航为「首页/探索/我的行程」，旧三链接不再出现', () => {
    const html = renderToStaticMarkup(createElement(TopNav, {}))
    expect(html).toContain('>首页</button>')
    expect(html).toContain('>探索</button>')
    expect(html).toContain('>我的行程</button>')
    expect(html).not.toContain('目的地')
    expect(html).not.toContain('旅行灵感')
    expect(html).not.toContain('旅行攻略')
  })
  it('「首页」项在 / 时 active；到 /explore 后让位给「探索」', () => {
    expect(renderToStaticMarkup(createElement(TopNav, {}))).toContain('class="top-nav-link active" type="button">首页</button>')
    window.history.pushState({}, '', '/explore')
    try {
      const html = renderToStaticMarkup(createElement(TopNav, {}))
      expect(html).toContain('class="top-nav-link active" type="button">探索</button>')
      expect(html).not.toContain('class="top-nav-link active" type="button">首页</button>')
    } finally {
      window.history.pushState({}, '', '/')
    }
  })
})

describe('TopNav 外观入口（2026-09-30 评审拍板：暗色样式早已存在，只缺入口）', () => {
  it('默认亮色：外观按钮以「切换到暗色」呈现，aria-pressed=false', () => {
    const html = renderToStaticMarkup(createElement(TopNav, {}))
    expect(html).toContain('切换到暗色')
    expect(html).toContain('aria-pressed="false"')
  })
})
