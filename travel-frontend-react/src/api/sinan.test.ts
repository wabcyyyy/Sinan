import { afterEach, describe, expect, it, vi } from 'vitest'
import { interpretImageIntent, reportClientError, streamItineraryEvents, waitForItinerary } from './sinan'

/** SSE 断流重连与错误探针的传输层测试：fetch 以鸭子类型打桩
 * （streamItineraryEvents 只消费 ok/status/body.getReader）。 */

interface StubResponse {
  ok: boolean
  status: number
  body: { getReader: () => { read: () => Promise<{ done: boolean; value?: Uint8Array }>; releaseLock: () => void } }
}

function sseStub(frames: string[], status = 200): StubResponse {
  const encoder = new TextEncoder()
  const chunks = frames.map((frame) => encoder.encode(frame))
  let index = 0
  return {
    ok: status >= 200 && status < 300,
    status,
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length ? { done: false, value: chunks[index++] } : { done: true, value: undefined },
        // 生产代码在 finally 里调 reader.releaseLock()：桩必须长得像真 reader
        releaseLock: () => {},
      }),
    },
  }
}

const event = (type: string) => `data:{"type":"${type}"}\n\n`

function stubFetchSequence(responses: Array<Promise<StubResponse> | Error>) {
  const calls: string[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    calls.push(String(input))
    const next = responses.shift()
    if (next instanceof Error) throw next
    return next
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('streamItineraryEvents 断流重连', () => {
  it('网络闪断后透明重连，终态帧后停', async () => {
    const { calls } = stubFetchSequence([
      new TypeError('network dropped'),
      Promise.resolve(sseStub([event('day_done'), event('done')])),
    ])
    const seen: string[] = []
    await streamItineraryEvents(
      1,
      new AbortController().signal,
      { onEvent: (e) => seen.push(e.type), onError: () => {} },
      { baseDelayMs: 1, maxReconnects: 2 },
    )
    expect(calls).toHaveLength(2)
    expect(seen).toEqual(['day_done', 'done'])
  })

  it('见到终态帧后的干净关流不再重连', async () => {
    const { calls } = stubFetchSequence([Promise.resolve(sseStub([event('done')]))])
    await streamItineraryEvents(
      1,
      new AbortController().signal,
      { onEvent: () => {}, onError: () => {} },
      { baseDelayMs: 1, maxReconnects: 2 },
    )
    expect(calls).toHaveLength(1)
  })

  it('4xx 立即失败不重试', async () => {
    const { calls } = stubFetchSequence([Promise.resolve(sseStub([], 401))])
    const onError = vi.fn()
    await expect(
      streamItineraryEvents(1, new AbortController().signal, { onEvent: () => {}, onError }, { baseDelayMs: 1 }),
    ).rejects.toThrow('401')
    expect(calls).toHaveLength(1)
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('重试耗尽后以最后错误收口，onError 只回调一次', async () => {
    stubFetchSequence([
      new TypeError('drop 1'),
      new TypeError('drop 2'),
      new TypeError('drop 3'),
    ])
    const onError = vi.fn()
    await expect(
      streamItineraryEvents(
        1,
        new AbortController().signal,
        { onEvent: () => {}, onError },
        { baseDelayMs: 1, maxReconnects: 2 },
      ),
    ).rejects.toThrow()
    expect(onError).toHaveBeenCalledTimes(1)
  })
})

describe('waitForItinerary 终态口径（M6：不再仅以 status==2 判断完全就绪）', () => {
  /** 详情轮询桩：apiRequest 走 response.text()，这里按应答序列回 JSON 信封。 */
  function stubDetailSequence(details: Array<Record<string, unknown>>) {
    const payloads = details.map((detail) => JSON.stringify({ code: 200, data: detail }))
    let index = 0
    const fetchMock = vi.fn(async () => {
      const raw = payloads[Math.min(index, payloads.length - 1)]
      index += 1
      return { ok: true, status: 200, text: async () => raw } as unknown as Response
    })
    vi.stubGlobal('fetch', fetchMock)
    return { fetchMock, calls: () => fetchMock.mock.calls.length }
  }

  const base = { id: 7, city: '成都', days: 3, dayList: [], budgetList: [], totalAmount: 0 }

  it('status=2 且 genState=PARTIAL：终态返回，不再误判"已全部完成"后无限轮询', async () => {
    stubDetailSequence([{ ...base, status: 2, genState: 'PARTIAL', coreReady: false }])
    const detail = await waitForItinerary(7, { intervalMs: 1 })
    expect(detail.genState).toBe('PARTIAL')
  })

  it('status=2 且 genState=CORE_READY：继续轮询直到 COMPLETED', async () => {
    const { calls } = stubDetailSequence([
      { ...base, status: 2, genState: 'CORE_READY', coreReady: true },
      { ...base, status: 2, genState: 'COMPLETED', coreReady: false },
    ])
    const detail = await waitForItinerary(7, { intervalMs: 1 })
    expect(detail.genState).toBe('COMPLETED')
    expect(calls()).toBe(2)
  })

  it('存量行（无 genState）保持旧口径：status=2 即完成', async () => {
    stubDetailSequence([{ ...base, status: 2 }])
    const detail = await waitForItinerary(7, { intervalMs: 1 })
    expect(detail.status).toBe(2)
  })

  it('status=3 仍抛错（取消/失败语义不变）', async () => {
    stubDetailSequence([{ ...base, status: 3, planNote: '生成已取消' }])
    await expect(waitForItinerary(7, { intervalMs: 1 })).rejects.toThrow('生成已取消')
  })
})

describe('reportClientError 会话防刷', () => {
  it('同源同消息去重、会话总量封顶、失败静默', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ code: 200, message: 'success', data: null }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    const report = (message: string, source: string) =>
      reportClientError({ message, source, stack: null, path: '/', userAgent: 'vitest', ts: 't' })

    report('boom', 'window')
    report('boom', 'window')
    await Promise.resolve()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/client-errors')

    // 封顶：Set 已有 1 条（boom），再灌 30 条唯一消息，总调用数停在 20
    for (let i = 0; i < 30; i += 1) report(`unique-${i}`, 'window')
    await Promise.resolve()
    expect(fetchMock).toHaveBeenCalledTimes(20)

    // 探针自身失败必须静默：不抛、不影响后续
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline') }))
    expect(() => report('late-error', 'window')).not.toThrow()
  })
})

describe('apiRequest FormData 分支（interpretImageIntent）', () => {
  const png = () => new File([new Uint8Array([1, 2, 3, 4])], 'shot.png', { type: 'image/png' })

  it('multipart 上传：body 为 FormData、不手写 JSON Content-Type、带可选 context', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ code: 200, message: 'success', data: { text: '一张成都街头的照片', suggestedMessage: '想去成都逛逛' } }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    const file = png()
    const vo = await interpretImageIntent(file, '在改成都的行程')
    expect(vo).toEqual({ text: '一张成都街头的照片', suggestedMessage: '想去成都逛逛' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/image-intent')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeInstanceOf(FormData)
    expect((init?.body as FormData).get('file')).toBe(file)
    expect((init?.body as FormData).get('context')).toBe('在改成都的行程')
    const headers = init?.headers as Record<string, string>
    expect(headers.Accept).toBe('application/json')
    // multipart boundary 由浏览器生成，手写 Content-Type 反而发不出去
    expect(headers['Content-Type']).toBeUndefined()
  })

  it('不传 context 时不追加该表单域', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ code: 200, message: 'success', data: { text: 't', suggestedMessage: 's' } }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    await interpretImageIntent(png())
    const [, init] = fetchMock.mock.calls[0]
    expect((init?.body as FormData).has('context')).toBe(false)
  })
})
