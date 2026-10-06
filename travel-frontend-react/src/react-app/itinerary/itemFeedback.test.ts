import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type * as Contracts from '../../types/generated/contracts'
import { buildFeedbackPayload, FEEDBACK_REASON_LABELS, FEEDBACK_REASON_ORDER, feedbackIndex } from './itemFeedback'
import { ItemFeedbackControl } from './ItemFeedbackControl'

/**
 * C3.5 条目反馈的口径与展示测试。交互归父层（TripDetailPage），这里测：
 * 1. 纯逻辑口径（与 app/schemas/feedback.py 的校验镜像）；
 * 2. 控制组件静态渲染的两态标记与选择器骨架（react-dom/server，同 TripBadges.test.ts 范式）。
 */

const vo = (fields: Partial<Contracts.FeedbackVO>): Contracts.FeedbackVO => ({
  itemId: 1,
  value: 'right',
  reason: null,
  note: null,
  createdAt: '2026-09-27T00:00:00',
  updatedAt: '2026-09-27T00:00:00',
  ...fields,
})

describe('反馈口径（镜像后端 FeedbackCreate 校验）', () => {
  it('right 恒 reason/note=null（误传也剥掉；契约 required-nullable，显式 null 与省略对后端等价）', () => {
    expect(buildFeedbackPayload(7, 'right')).toEqual({ itemId: 7, value: 'right', reason: null, note: null })
    expect(buildFeedbackPayload(7, 'right', 'wrong_price', '贵了')).toEqual({ itemId: 7, value: 'right', reason: null, note: null })
  })

  it('wrong 未选原因返回 null（不发请求，选择器禁用提交）', () => {
    expect(buildFeedbackPayload(7, 'wrong')).toBeNull()
  })

  it('wrong 带 reason；note 可选且去空白（无 note 时显式 null）', () => {
    expect(buildFeedbackPayload(7, 'wrong', 'closed')).toEqual({ itemId: 7, value: 'wrong', reason: 'closed', note: null })
    expect(buildFeedbackPayload(7, 'wrong', 'other', '  已拆了  ')).toEqual({ itemId: 7, value: 'wrong', reason: 'other', note: '已拆了' })
    expect(buildFeedbackPayload(7, 'wrong', 'other', '   ')).toEqual({ itemId: 7, value: 'wrong', reason: 'other', note: null })
  })

  it('原因枚举中英映射一一对应', () => {
    expect(FEEDBACK_REASON_ORDER).toHaveLength(6)
    for (const key of FEEDBACK_REASON_ORDER) expect(FEEDBACK_REASON_LABELS[key]).toBeTruthy()
  })

  it('回显列表按 itemId 索引（一人一条无冲突）', () => {
    const map = feedbackIndex([vo({ itemId: 3, value: 'wrong' }), vo({ itemId: 5, value: 'right' })])
    expect(map.get(3)?.value).toBe('wrong')
    expect(map.get(5)?.value).toBe('right')
    expect(map.size).toBe(2)
    expect(feedbackIndex(undefined).size).toBe(0)
  })
})

describe('ItemFeedbackControl 控制渲染', () => {
  it('未标记：两个切换钮、无激活态、无选择器', () => {
    const html = renderToStaticMarkup(
      createElement(ItemFeedbackControl, { itemId: 9, feedback: undefined, onSet: async () => {}, onRevoke: async () => {} }),
    )
    expect(html).toContain('没问题')
    expect(html).toContain('有误')
    expect(html).not.toContain('aria-pressed="true"')
    expect(html).not.toContain('fb-picker')
  })

  it('已标 right：激活态可见', () => {
    const html = renderToStaticMarkup(
      createElement(ItemFeedbackControl, { itemId: 9, feedback: vo({ value: 'right' }), onSet: async () => {}, onRevoke: async () => {} }),
    )
    expect(html).toContain('aria-pressed="true"')
    expect(html).toContain('active')
  })
})
