import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { appHref, appPath, hasIdentity, storageKey } from '../../demoMode'

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); localStorage.clear(); window.history.replaceState({}, '', '/demo') })
afterEach(() => { vi.useRealTimers(); window.history.replaceState({}, '', '/') })
const request = async (path: string, body?: unknown, method = 'POST') => {
  const { demoFetch } = await import('./transport')
  const response = await demoFetch('/api' + path, body ? { method, body: JSON.stringify(body) } : {})
  return { response, ...(await response.json()) }
}

it('starts each edit tour from an independent sample without overwriting existing work', async () => {
  const { createEditTour } = await import('./transport')
  const first = createEditTour()
  const original = (await request('/itinerary/101')).data
  const draft = (await request(`/itinerary/${first}/chat-edit`, { message: '把第二天安排松一点' })).data
  await request(`/itinerary/${first}/apply-plans`, { actionMessageId: draft.messageId, baseRevision: draft.baseRevision })
  const second = createEditTour()
  expect(second).not.toBe(first)
  expect((await request(`/itinerary/${first}`)).data.totalAmount).toBe(1080)
  expect((await request(`/itinerary/${second}`)).data.totalAmount).toBe(1180)
  expect((await request('/itinerary/101')).data).toEqual(original)
})

it('copies editorial routes into separate tab-local trips and edits only the confirmed copy', async () => {
  const before = (await request('/templates/-1')).data
  const copy = (await request('/templates/-1/fork', {})).data
  const originalCopy = (await request(`/itinerary/${copy.itineraryId}`)).data
  expect(originalCopy.city).toBe('杭州')
  const draft = (await request(`/itinerary/${copy.itineraryId}/chat-edit`, { message: '把第二天安排松一点' })).data
  const applied = (await request(`/itinerary/${copy.itineraryId}/apply-plans`, { actionMessageId: draft.messageId, baseRevision: draft.baseRevision })).data
  expect(applied.dayList[0]).toEqual(originalCopy.dayList[0])
  expect(applied.dayList[1].items.length).toBe(originalCopy.dayList[1].items.length - 1)
  expect((await request('/templates/-1')).data).toEqual(before)
  vi.resetModules()
  expect((await request(`/itinerary/${copy.itineraryId}`)).data.dayList[1].items).toEqual(applied.dayList[1].items)
})

describe('真实页面的固定样例数据入口', () => {
  it('复用路由并隔离演示身份与草稿，不覆盖已有账号', () => {
    localStorage.setItem('sinan-username', 'real-user')
    expect(hasIdentity()).toBe(true)
    expect(appHref('/trips/101')).toBe('/demo/trips/101')
    expect(appPath('/demo/trips/101')).toBe('/trips/101')
    expect(storageKey('sinan-intake-v1')).toBe('sinan-demo:sinan-intake-v1')
    expect(localStorage.getItem('sinan-username')).toBe('real-user')
    window.history.replaceState({}, '', '/')
    expect(appHref('/trips/101')).toBe('/trips/101')
    expect(storageKey('sinan-intake-v1')).toBe('sinan-intake-v1')
  })

  it('新版草稿存储仍隔离展示历史，保留真实身份和草稿', async () => {
    const { activateIntakeOwner, intakeSessionStorage, intakeHistoryStorage } = await import('../home/intakeStorage')
    localStorage.setItem('sinan-username', 'real-user')
    localStorage.setItem('sinan-intake-v2:user-7:history', 'real history')
    sessionStorage.setItem('sinan-intake-v2:user-7:current', 'real draft')
    activateIntakeOwner(0)
    intakeSessionStorage.setItem('current', 'demo draft')
    intakeHistoryStorage.setItem('history', 'demo history')
    expect(sessionStorage.getItem('sinan-demo:sinan-intake-v2:user-0:current')).toBe('demo draft')
    expect(sessionStorage.getItem('sinan-demo:sinan-intake-v2:user-0:history')).toBe('demo history')
    expect(localStorage.getItem('sinan-demo:sinan-intake-v2:user-0:history')).toBeNull()
    activateIntakeOwner(0)
    expect(intakeSessionStorage.getItem('current')).toBe('demo draft')
    expect(localStorage.getItem('sinan-username')).toBe('real-user')
    expect(localStorage.getItem('sinan-intake-v2:user-7:history')).toBe('real history')
    expect(sessionStorage.getItem('sinan-intake-v2:user-7:current')).toBe('real draft')
  })

  it('通过正式 API 消费样例，既不联网也不把其他城市假装生成成功', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('must not use network'))
    const api = await import('../../api/sinan')
    expect((await api.getItineraryDetail(101)).dayList).toHaveLength(2)
    expect((await api.clarifyItinerary('想去杭州', {}, null)).ready).toBe(false)
    expect((await api.clarifyItinerary('成都两天', {}, null)).ready).toBe(true)
    await expect(api.generateItinerary({ city: '杭州', days: 2, persons: 2, stayNights: 1, preferences: [] }, 'example')).rejects.toThrow('固定脚本')
    expect(fetch).not.toHaveBeenCalled()
    fetch.mockRestore()
  })

  it('生成快照逐日交付，重新加载数据入口仍能恢复当前进度', async () => {
    vi.useFakeTimers()
    const generated = await request('/itinerary/generate', { city: '成都', days: 2, persons: 2 })
    expect(generated.data.genState).toBe('GENERATING')
    expect(generated.data.dayList[0].items).toEqual([])
    vi.advanceTimersByTime(2300)
    expect((await request('/itinerary/102')).data.dayList[0].items.length).toBeGreaterThan(3)
    expect((await request('/itinerary/102')).data.dayList[1].items).toEqual([])
    vi.resetModules()
    vi.advanceTimersByTime(2300)
    expect((await request('/itinerary/102')).data.genState).toBe('COMPLETED')
    expect((await request('/itinerary')).data).toHaveLength(2)
  })

  it('编辑先产生差异草稿，确认后只改第二天；预算重算且过期确认被拒绝', async () => {
    const before = (await request('/itinerary/101')).data
    expect(before.totalAmount).toBe(1180)
    const draft = (await request('/itinerary/101/chat-edit', { message: '把第二天安排松一点' })).data
    expect((await request('/itinerary/101')).data).toEqual(before)
    const applied = (await request('/itinerary/101/apply-plans', { actionMessageId: draft.messageId, baseRevision: draft.baseRevision })).data
    expect(applied.dayList[0]).toEqual(before.dayList[0])
    expect(applied.dayList[1].items.some((item: { poiName: string }) => item.poiName === '杜甫草堂')).toBe(false)
    expect(applied.totalAmount).toBe(1080)
    expect((await request('/itinerary/101/apply-plans', { actionMessageId: draft.messageId, baseRevision: draft.baseRevision })).response.status).toBe(409)
    vi.resetModules()
    expect((await request('/itinerary/101')).data.totalAmount).toBe(applied.totalAmount)
  })

  it('收藏和当天标题可交互；分享与密钥保存明确拒绝', async () => {
    expect((await request('/itinerary/101/favorite', { favorite: false })).data.favorite).toBe(false)
    expect((await request('/itinerary/101/days/1', { theme: '我的慢游标题' }, 'PATCH')).data.dayList[0].theme).toBe('我的慢游标题')
    expect((await request('/itinerary/101/share', { expireDays: 7 })).response.status).toBe(400)
    expect((await request('/llm-gateway', { apiKey: 'example' })).response.status).toBe(400)
  })

  it('未知行程返回明确错误，不回退到另一条样例', async () => {
    expect((await request('/itinerary/999')).response.status).toBe(404)
  })

  it('脚本不支持的编辑指令不能被错误地应用到第二天', async () => {
    const before = (await request('/itinerary/101')).data
    const reply = (await request('/itinerary/101/chat-edit', { message: '把第1天安排松一点' })).data
    expect(reply.plans).toBeUndefined()
    expect(reply.content).toContain('固定脚本')
    expect((await request('/itinerary/101')).data).toEqual(before)
  })
})
