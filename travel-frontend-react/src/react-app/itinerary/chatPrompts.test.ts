import { describe, expect, it } from 'vitest'
import { excludedTermsFromMessages, getDayQuickPrompts, getPromptSceneGroups } from './chatPrompts'

describe('chatPrompts（上下文感知快捷指令）', () => {
  it('getDayQuickPrompts：按天数动态生成各类场景快捷指令', () => {
    const list = getDayQuickPrompts(2, '成都')
    expect(list.length).toBeGreaterThanOrEqual(4)
    expect(list.some((item) => item.label.includes('第 2 天') && item.category === 'route')).toBe(true)
    expect(list.some((item) => item.category === 'food')).toBe(true)
    expect(list.some((item) => item.prompt.includes('成都'))).toBe(true)
  })

  it('getPromptSceneGroups：空状态生成精选分组推荐', () => {
    const groups = getPromptSceneGroups(3, '西安')
    expect(groups.length).toBe(3)
    expect(groups[0].sceneTitle).toContain('路线')
    expect(groups[0].prompts.some((p) => p.includes('第 3 天'))).toBe(true)
  })

  // ---------- M6（spec §12）：按近期对话排除过滤 ----------

  it('无排除上下文时返回全量建议', () => {
    const full = getDayQuickPrompts(1, '杭州')
    expect(getDayQuickPrompts(1, '杭州', { recentMessages: [] })).toEqual(full)
    expect(getDayQuickPrompts(1, '杭州', { recentMessages: [{ role: 'user', content: '把节奏放慢一点' }] })).toEqual(full)
  })

  it('「不要博物馆」后不再推荐博物馆类建议（M6）', () => {
    const full = getDayQuickPrompts(2)
    expect(full.some((item) => item.prompt.includes('博物馆'))).toBe(true)
    const filtered = getDayQuickPrompts(2, '杭州', {
      recentMessages: [
        { role: 'user', content: '不要博物馆，其他都行' },
        { role: 'ai', content: '好的：博物馆已排除，我会避开这类场馆。' },
      ],
    })
    expect(filtered.some((item) => item.prompt.includes('博物馆'))).toBe(false)
    expect(filtered.length).toBe(full.length - 1)
  })

  it('「不去+地点名」提取地名并过滤含该地名的建议', () => {
    expect(excludedTermsFromMessages([{ role: 'user', content: '第二天不去灵隐寺' }])).toContain('灵隐寺')
    // 内建建议不含具体地点名 → 过滤为 no-op（不多不少）
    const full = getDayQuickPrompts(1)
    expect(getDayQuickPrompts(1, undefined, { recentMessages: [{ role: 'user', content: '不去灵隐寺' }] })).toEqual(full)
  })

  it('只扫描最近 6 条消息，更早的排除不再生效', () => {
    const messages = [
      { role: 'user', content: '不要博物馆' },
      ...Array.from({ length: 6 }, (_, i) => ({ role: 'ai' as const, content: `第 ${i} 轮无关内容` })),
    ]
    expect(excludedTermsFromMessages(messages)).toEqual([])
  })

  it('多条排除项一起生效；无内容/非文本消息安全跳过', () => {
    const terms = excludedTermsFromMessages([
      { role: 'user', content: '不要博物馆，不去西湖' },
      { role: 'user', content: '' },
      { role: 'ai', content: undefined as unknown as string },
    ] as Array<{ role: string; content: string }>)
    expect(terms).toEqual(expect.arrayContaining(['博物馆', '西湖']))
    const filtered = getDayQuickPrompts(1, '杭州', {
      recentMessages: [{ role: 'user', content: '不要博物馆' }],
    })
    expect(filtered.every((item) => !item.prompt.includes('博物馆'))).toBe(true)
  })
})
