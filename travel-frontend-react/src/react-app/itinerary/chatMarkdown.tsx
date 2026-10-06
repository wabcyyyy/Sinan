/**
 * 聊天气泡最小 Markdown 渲染：后端编排文案会带 `### 标题` 与 `**加粗**`
 * （如「### 新安排存在时间冲突」），原文直出曾把标记泄漏给用户。
 *
 * 只做这两个子集（够用、零新依赖）；构建的是 React 文本节点而非 innerHTML，
 * 天然无注入面。出现更复杂需求时再换成熟渲染器并在 commit 里给理由。
 */
import { Fragment, createElement } from 'react'
import type { ReactNode } from 'react'

const HEADING = /^(#{1,6})\s+(.*)$/
const BOLD = /\*\*([^*]+)\*\*/g

/** 行内 `**bold**` → <strong>；无标记的行原样返回（保留 \\n 交给 pre-wrap）。 */
function renderInline(line: string): ReactNode {
  const parts = line.split(BOLD)
  if (parts.length === 1) return line
  return parts.map((part, index) => (index % 2 === 1 ? createElement('strong', { key: index }, part) : part))
}

/** AI 气泡用：整段消息 → 标题行 <strong class="chat-md-h">（块级）+ 行内加粗。 */
export function renderChatMarkdown(text: string): ReactNode {
  if (!text) return text
  const out: ReactNode[] = []
  let lastWasHeading = false
  text.split('\n').forEach((line, index) => {
    const heading = HEADING.exec(line.trim())
    if (heading) {
      out.push(createElement('strong', { className: 'chat-md-h', key: `h-${index}` }, renderInline(heading[2])))
      lastWasHeading = true
      return
    }
    // 标题行是块级元素自带换行；普通行之间补回 \n 交给气泡的 pre-wrap
    if (index > 0 && !lastWasHeading) out.push('\n')
    out.push(createElement(Fragment, { key: `t-${index}` }, renderInline(line)))
    lastWasHeading = false
  })
  return out
}
