import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearAllIntakeSessions,
  deleteIntakeSession,
  deriveSessionTitle,
  formatSessionTime,
  listIntakeSessions,
  saveIntakeSession,
} from './intakeHistory'

describe('intakeHistory（对话管理与历史会话草稿）', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('空会话列表返回 []', () => {
    expect(listIntakeSessions()).toEqual([])
  })

  it('无有效用户消息或信息时不保存无效空草稿', () => {
    const res = saveIntakeSession({
      slots: {},
      messages: [{ id: 'greeting', role: 'assistant', text: 'hi' }],
      firstMessage: '',
    })
    expect(res).toBeNull()
    expect(listIntakeSessions()).toHaveLength(0)
  })

  it('保存有内容的会话草稿并自动生成标题', () => {
    const record = saveIntakeSession({
      slots: { city: '成都', days: 4, persons: 2 },
      messages: [
        { id: 'g', role: 'assistant', text: '想去哪儿？' },
        { id: 'u1', role: 'user', text: '国庆想去成都' },
      ],
      firstMessage: '国庆想去成都',
    })
    expect(record).not.toBeNull()
    expect(record?.title).toBe('成都 · 4天 · 2人规划')

    const list = listIntakeSessions()
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe(record?.id)
    expect(list[0].slots.city).toBe('成都')
  })

  it('支持根据 id 覆盖更新同一会话', () => {
    const first = saveIntakeSession({
      slots: { city: '成都' },
      messages: [{ id: 'u1', role: 'user', text: '去成都' }],
      firstMessage: '去成都',
    })
    expect(first).not.toBeNull()

    const updated = saveIntakeSession({
      id: first!.id,
      slots: { city: '成都', days: 3 },
      messages: [
        { id: 'u1', role: 'user', text: '去成都' },
        { id: 'u2', role: 'user', text: '玩3天' },
      ],
      firstMessage: '去成都',
    })
    expect(updated?.id).toBe(first!.id)
    expect(updated?.slots.days).toBe(3)

    const list = listIntakeSessions()
    expect(list).toHaveLength(1)
    expect(list[0].slots.days).toBe(3)
  })

  it('deleteIntakeSession 删除指定会话', () => {
    const s1 = saveIntakeSession({
      slots: { city: '成都' },
      messages: [{ id: 'u', role: 'user', text: '成都' }],
      firstMessage: '成都',
    })
    saveIntakeSession({
      slots: { city: '西安' },
      messages: [{ id: 'u', role: 'user', text: '西安' }],
      firstMessage: '西安',
    })
    expect(listIntakeSessions()).toHaveLength(2)

    deleteIntakeSession(s1!.id)
    const list = listIntakeSessions()
    expect(list).toHaveLength(1)
    expect(list[0].slots.city).toBe('西安')
  })

  it('clearAllIntakeSessions 清空所有草稿', () => {
    saveIntakeSession({
      slots: { city: '成都' },
      messages: [{ id: 'u', role: 'user', text: '成都' }],
      firstMessage: '成都',
    })
    clearAllIntakeSessions()
    expect(listIntakeSessions()).toEqual([])
  })

  it('deriveSessionTitle 与 formatSessionTime 工具函数', () => {
    expect(deriveSessionTitle({ city: '杭州', days: 3 }, '')).toBe('杭州 · 3天规划')
    expect(deriveSessionTitle({}, '想去海边吹吹风放松一下')).toBe('想去海边吹吹风放松一下')
    expect(deriveSessionTitle({}, '')).toBe('未命名旅行想法')

    expect(formatSessionTime(Date.now() - 1000)).toBe('刚刚')
    expect(formatSessionTime(Date.now() - 5 * 60 * 1000)).toBe('5 分钟前')
  })
})
