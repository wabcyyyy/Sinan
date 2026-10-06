import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { renderChatMarkdown } from './chatMarkdown'

/** 最小 Markdown 子集（### 标题 + **加粗**）：标记不泄漏、结构可断言（同域静态渲染范式）。 */
describe('renderChatMarkdown', () => {
  it('### 标题渲染成块级 strong，不再泄漏 # 号', () => {
    const html = renderToStaticMarkup(createElement('p', null, renderChatMarkdown('### 新安排存在时间冲突')))
    expect(html).toContain('chat-md-h')
    expect(html).toContain('新安排存在时间冲突')
    expect(html).not.toContain('#')
  })

  it('行内 **加粗** 转 strong，正文原样保留', () => {
    const html = renderToStaticMarkup(createElement('p', null, renderChatMarkdown('已为你保留 **2 天** 的博物馆安排')))
    expect(html).toContain('<strong>2 天</strong>')
    expect(html).not.toContain('**')
    expect(html).toContain('已为你保留')
  })

  it('标题行内仍可加粗；普通行之间保留换行（pre-wrap 生效）', () => {
    const html = renderToStaticMarkup(createElement('p', null, renderChatMarkdown('## 冲突提醒\n第 **2 天** 已排满\n换个日期更好')))
    expect(html).toContain('chat-md-h')
    expect(html).toContain('<strong>2 天</strong>')
    expect(html).toContain('\n换个日期更好')
  })

  it('无标记文本与空串原样返回，不做任何加工', () => {
    expect(renderChatMarkdown('')).toBe('')
    const html = renderToStaticMarkup(createElement('p', null, renderChatMarkdown('普通一句话')))
    expect(html).toBe('<p>普通一句话</p>')
  })
})
