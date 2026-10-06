import { describe, expect, it } from 'vitest'
import { getDayQuickPrompts, getPromptSceneGroups } from './chatPrompts'

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
})
