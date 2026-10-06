import { beforeAll, describe, expect, it, vi } from 'vitest'
import { installErrorProbe } from './errorProbe'

/** 全局探针装线测试：dispatch 真实 DOM 事件，fetch 打桩断言上报形状。 */

interface ReportedBody {
  message: string
  source: string | null
  stack: string | null
  path: string
}

function lastReported(fetchMock: ReturnType<typeof vi.fn>): ReportedBody {
  const call = fetchMock.mock.calls.at(-1)
  const init = call?.[1] as RequestInit | undefined
  return JSON.parse(String(init?.body)) as ReportedBody
}

describe('installErrorProbe', () => {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ code: 200, message: 'success', data: null }),
  }))

  beforeAll(() => {
    vi.stubGlobal('fetch', fetchMock)
    installErrorProbe()
  })

  it('window error 事件按形状上报', () => {
    window.dispatchEvent(new ErrorEvent('error', { message: 'Uncaught TypeError: boom', error: new Error('boom') }))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const body = lastReported(fetchMock)
    expect(body.message).toBe('Uncaught TypeError: boom')
    expect(body.source).toBe('window')
    expect(body.stack).toContain('Error: boom')
    expect(body.path).toBe('/')
  })

  it('资源加载失败（非 ErrorEvent）不上报', () => {
    window.dispatchEvent(new Event('error'))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('unhandledrejection 的 Error reason 提取 message 与 stack', () => {
    const rejection = new Event('unhandledrejection') as Event & { reason: unknown }
    rejection.reason = new Error('async boom')
    window.dispatchEvent(rejection)
    const body = lastReported(fetchMock)
    expect(body.message).toBe('async boom')
    expect(body.source).toBe('unhandledrejection')
  })

  it('同一错误的重复触发被会话去重', () => {
    const before = fetchMock.mock.calls.length
    window.dispatchEvent(new ErrorEvent('error', { message: 'Uncaught TypeError: boom', error: new Error('boom') }))
    window.dispatchEvent(new ErrorEvent('error', { message: 'Uncaught TypeError: boom', error: new Error('boom') }))
    expect(fetchMock.mock.calls.length).toBe(before)
  })
})
