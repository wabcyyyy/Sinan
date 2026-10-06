import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { SmartImg } from './SmartImg'

// vitest include 只认 *.test.ts（不能写 JSX），用 createElement 断言渲染产物。
const h = (props: Parameters<typeof SmartImg>[0]) => renderToStaticMarkup(createElement(SmartImg, props))

describe('SmartImg', () => {
  it('默认懒加载 + 异步解码，并带占位盒', () => {
    const html = h({ src: '/a.jpg', alt: '测试图' })
    expect(html).toContain('loading="lazy"')
    expect(html).toContain('decoding="async"')
    expect(html).toContain('class="smart-img "')
    expect(html).toContain('data-state="loading"')
    expect(html).toContain('alt="测试图"')
  })

  it('eager 用于首屏：eager 加载 + 高抓取优先级', () => {
    const html = h({ src: '/hero.jpg', alt: '首屏', eager: true })
    expect(html).toContain('loading="eager"')
    expect(/fetchpriority="high"/i.test(html)).toBe(true)
  })

  it('ratio 落在盒子的 aspect-ratio 内联样式上', () => {
    const html = h({ src: '/a.jpg', alt: '比例', ratio: '4 / 3' })
    expect(html).toContain('aspect-ratio:4 / 3')
  })
})
