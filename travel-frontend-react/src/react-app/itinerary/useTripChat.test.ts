import { act } from 'react'
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { chatEditItinerary, chatEditStream, getItineraryChatHistory, ReactApiError } from '../../api/sinan'
import type { ChatReply } from '../../api/sinan'
import { useTripChat } from './useTripChat'

/**
 * M6（spec §12 编辑入口）turn 幂等的前端消费测试：mock api 层捕获 X-Turn-Id 入参，
 * 断言——同一 send 的流式→阻塞回退复用同一 turnId；用户新发才是新 turnId；
 * 409「仍在处理」呈现等待提示且不回退重发；409「已用于不同请求」按错误透出；
 * done 重放（changed=false + 固定 reply）按现有 changed=false 路径渲染。
 * 后端语义真源是 BE app/services/itinerary_chat.py 的 claim_chat_turn。
 */

vi.mock('../../api/sinan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sinan')>()
  return {
    ...actual,
    getItineraryChatHistory: vi.fn(),
    chatEditStream: vi.fn(),
    chatEditItinerary: vi.fn(),
    applyPlans: vi.fn(),
    applyHotelOption: vi.fn(),
  }
})

const historyMock = vi.mocked(getItineraryChatHistory)
const streamMock = vi.mocked(chatEditStream)
const blockingMock = vi.mocked(chatEditItinerary)

const STILL_PROCESSING_409 = new ReactApiError('同一指令仍在处理中，请等待完成或稍后查询', 409)
const CONFLICT_409 = new ReactApiError('同一 turnId 已用于不同请求，请更换后重试', 409)

const replayReply: ChatReply = {
  reply: '这条刚刚已经处理过了，行程未变化',
  changed: false,
  plans: [],
  baseRevision: 'rev-1',
  requiresConfirmation: false,
}

const draftReply: ChatReply = {
  reply: '已把灵隐寺挪到第一天',
  changed: true,
  plans: [{ day_no: 1, items: [{ poi_name: '灵隐寺', item_type: 'attraction' }] }],
  baseRevision: 'rev-2',
  requiresConfirmation: true,
}

/** hook 状态探针：msgs/notice 投影成 JSON 供断言。 */
function ChatProbe() {
  const chat = useTripChat(7, () => {}, () => {})
  return createElement(
    'div',
    null,
    createElement('button', { type: 'button', className: 'probe-send', onClick: () => void chat.send('把灵隐寺挪到第一天') }, 'send'),
    createElement(
      'output',
      { className: 'probe-state' },
      JSON.stringify({
        sending: chat.sending,
        notice: chat.notice,
        msgs: chat.msgs.map((msg) => ({ role: msg.role, content: msg.content, changed: msg.changed })),
      }),
    ),
  )
}

const roots: Array<{ root: Root; container: HTMLDivElement }> = []

async function flush() {
  await act(async () => {})
}

async function mountProbe() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  roots.push({ root, container })
  await act(async () => { root.render(createElement(ChatProbe)) })
  await flush()
  return container
}

async function send(container: HTMLDivElement) {
  await act(async () => { (container.querySelector('.probe-send') as HTMLButtonElement).click() })
  await flush()
}

const stateOf = (container: HTMLDivElement) =>
  JSON.parse(container.querySelector('.probe-state')!.textContent ?? '{}') as {
    sending: boolean
    notice: string
    msgs: Array<{ role: string; content: string; changed?: boolean }>
  }

/** 第 n 次 send 的流式 turnId（chatEditStream 第 6 参）。 */
const streamTurnId = (n: number) => streamMock.mock.calls[n][5] as string | undefined
/** 第 n 次阻塞回退的 turnId（chatEditItinerary 第 5 参）。 */
const blockingTurnId = (n: number) => blockingMock.mock.calls[n][4] as string | undefined

describe('useTripChat M6 turn 幂等（spec §12 编辑入口）', () => {
  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    historyMock.mockReset(); historyMock.mockResolvedValue([])
    streamMock.mockReset(); streamMock.mockResolvedValue(undefined)
    blockingMock.mockReset()
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
  })

  it('send 生成 turnId 并随流式请求带出；新消息新 turnId', async () => {
    const container = await mountProbe()
    await send(container)
    await send(container)
    expect(streamMock).toHaveBeenCalledTimes(2)
    const first = streamTurnId(0)
    expect(first).toBeTruthy()
    expect(streamTurnId(1)).toBeTruthy()
    expect(streamTurnId(1)).not.toBe(first)
    expect(stateOf(container).sending).toBe(false)
  })

  it('流式失败回退阻塞通道：同一 send 复用同一 turnId（断流恢复不跑第二份）', async () => {
    streamMock.mockRejectedValueOnce(new TypeError('network down'))
    blockingMock.mockResolvedValueOnce(draftReply)
    const container = await mountProbe()
    await send(container)
    expect(streamMock).toHaveBeenCalledTimes(1)
    expect(blockingMock).toHaveBeenCalledTimes(1)
    expect(blockingTurnId(0)).toBe(streamTurnId(0))
    // 回退成功按普通回合渲染草稿
    const msgs = stateOf(container).msgs
    expect(msgs[msgs.length - 1].content).toContain('已把灵隐寺挪到第一天')
  })

  it('409「仍在处理」：等待提示 + 占位保留，不回退阻塞重发', async () => {
    streamMock.mockRejectedValueOnce(STILL_PROCESSING_409)
    const container = await mountProbe()
    await send(container)
    expect(blockingMock).not.toHaveBeenCalled()
    const state = stateOf(container)
    expect(state.notice).toContain('仍在处理')
    const ai = state.msgs.find((msg) => msg.role === 'ai')
    expect(ai?.content).toContain('还在处理中')
  })

  it('409「已用于不同请求」：错误透出、占位移除，同样不回退重发', async () => {
    streamMock.mockRejectedValueOnce(CONFLICT_409)
    const container = await mountProbe()
    await send(container)
    expect(blockingMock).not.toHaveBeenCalled()
    const state = stateOf(container)
    expect(state.notice).toContain('已用于不同请求')
    expect(state.msgs.find((msg) => msg.role === 'ai')).toBeUndefined()
  })

  it('流式挂了之后阻塞回退也撞 409：仍按冲突语义呈现，不崩、不再重发', async () => {
    streamMock.mockRejectedValueOnce(new TypeError('network down'))
    blockingMock.mockRejectedValueOnce(STILL_PROCESSING_409)
    const container = await mountProbe()
    await send(container)
    expect(blockingMock).toHaveBeenCalledTimes(1)
    const state = stateOf(container)
    expect(state.notice).toContain('仍在处理')
    expect(state.sending).toBe(false)
  })

  it('done 重放（changed=false + 固定 reply）经回退按现有 changed=false 路径渲染', async () => {
    streamMock.mockRejectedValueOnce(new TypeError('network down'))
    blockingMock.mockResolvedValueOnce(replayReply)
    const container = await mountProbe()
    await send(container)
    const state = stateOf(container)
    const ai = state.msgs.find((msg) => msg.role === 'ai')
    expect(ai?.content).toContain('这条刚刚已经处理过了')
    expect(ai?.changed).toBe(false)
  })
})
