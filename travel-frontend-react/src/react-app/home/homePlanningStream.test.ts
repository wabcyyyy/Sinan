import { act } from 'react'
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { generateItinerary, getItineraryDetail, streamItineraryEvents, waitForItinerary } from '../../api/sinan'
import type { ItineraryStreamEvent, StreamHandlers, WaitForItineraryOptions } from '../../api/sinan'
import type { ItineraryDetail } from '../../types/itinerary'
import { candidatesForDay, emptyPreviewState, prunePreviewsByDraft, reduceItemPreviewEvent } from './itemPreviews'
import type { ItemPreviewCandidate, ItemPreviewState } from './itemPreviews'
import { useHomePlanning } from './useHomePlanning'
import type { IntakeChat } from './useIntakeChat'
import { GREETING } from './intakeSlots'
import { TripBoard } from './TripBoard'
import { TripPanel } from './TripPanel'

/**
 * M5a/M5b 流事件消费测试（spec §10.2/§11）：业务面 item_preview / item_preview_withdrawn /
 * core_ready / stage_timing 帧 → useHomePlanning 状态 → 预览板渲染。三层拆分：
 * - itemPreviews 纯 reducer 单测（去重/撤回/陈旧 run/快照让位）；
 * - hook 级 mock api 交互（真 useHomePlanning + mock 流事件序列，同 homeIntake.test.ts 纪律）；
 * - 静态渲染断言（renderToStaticMarkup，零新依赖）。
 * 业务面帧 data 是开放 dict（帧表真源在 BE generation_events.py），按消费键构造。
 * M6（spec §12 创建入口）另含 attempt 幂等块：X-Idempotency-Key 的持久化/复用/换新。
 */

vi.mock('../../api/sinan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sinan')>()
  return {
    ...actual,
    generateItinerary: vi.fn(),
    getItineraryDetail: vi.fn(),
    waitForItinerary: vi.fn(),
    streamItineraryEvents: vi.fn(),
  }
})

const generateMock = vi.mocked(generateItinerary)
const getDetailMock = vi.mocked(getItineraryDetail)
const waitForMock = vi.mocked(waitForItinerary)
const streamMock = vi.mocked(streamItineraryEvents)

// ===== 帧构造（业务面信封五键，data 开放 dict 按消费键给值）=====

const previewFrame = (previewId: string, dayNo: number, ordinal: number, poiName: string, runId = 'run-1'): ItineraryStreamEvent => ({
  type: 'item_preview',
  itineraryId: 7,
  seq: 1,
  ts: '2026-10-09T00:00:00Z',
  data: { runId, previewId, dayNo, itemOrdinal: ordinal, item: { itemType: 'attraction', poiName } },
})

const withdrawFrame = (previewId: string, dayNo: number, ordinal: number, runId = 'run-1'): ItineraryStreamEvent => ({
  type: 'item_preview_withdrawn',
  itineraryId: 7,
  seq: 2,
  ts: '2026-10-09T00:00:01Z',
  data: { runId, previewId, dayNo, itemOrdinal: ordinal, reason: 'leg_failed' },
})

const namesOf = (state: ItemPreviewState) => state.candidates.map((candidate) => candidate.item.poiName)

// ===== 1. 纯 reducer：去重 / 撤回 / 陈旧 run / 快照整日让位 =====

describe('itemPreviews reducer（M5a 候选状态机）', () => {
  it('item_preview 追加候选；重复 previewId 不重复追加', () => {
    let state = emptyPreviewState()
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:2', 1, 2, '人民公园'))
    expect(namesOf(state)).toEqual(['宽窄巷子', '人民公园'])
    // 同一候选帧重放（断线重连/转发重复）：身份去重，引用不变不触发多余渲染
    const before = state
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    expect(state.candidates).toHaveLength(2)
    expect(state).toBe(before)
  })

  it('item_preview_withdrawn 按 previewId 精确移除；非当前 run 的撤回忽略', () => {
    let state = emptyPreviewState()
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:2', 1, 2, '人民公园'))
    state = reduceItemPreviewEvent(state, withdrawFrame('run-1:1:1', 1, 1))
    expect(namesOf(state)).toEqual(['人民公园'])
    // 旧 run 的撤回（其候选已随换 run 作废）不消费
    state = reduceItemPreviewEvent(state, withdrawFrame('run-0:1:9', 1, 9, 'run-0'))
    expect(namesOf(state)).toEqual(['人民公园'])
  })

  it('换 run（runId 变化）：旧 run 残余候选作废切换新 run；旧 run 迟到帧忽略不回滚', () => {
    let state = emptyPreviewState()
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    // 全新 run 出现：旧候选整体作废，只留新 run 候选
    state = reduceItemPreviewEvent(state, previewFrame('run-2:2:1', 2, 1, '灵隐寺', 'run-2'))
    expect(namesOf(state)).toEqual(['灵隐寺'])
    expect(state.runId).toBe('run-2')
    // 旧 run 的迟到帧：陈旧 run 忽略，不回滚成 run-1、不丢 run-2 候选
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:3', 1, 3, '迟到帧', 'run-1'))
    expect(namesOf(state)).toEqual(['灵隐寺'])
    expect(state.runId).toBe('run-2')
  })

  it('缺 runId / 未知帧 / stage_timing：原样透传不改状态（M5b 观测帧前端暂不展示）', () => {
    let state = emptyPreviewState()
    state = reduceItemPreviewEvent(state, { type: 'item_preview', data: { dayNo: 1, item: { poiName: 'x' } } })
    expect(state.candidates).toHaveLength(0)
    const before = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    const after = reduceItemPreviewEvent(before, {
      type: 'stage_timing',
      itineraryId: 7,
      data: { stage: 'core_ready', elapsedMs: 1200 },
    })
    expect(after).toBe(before)
  })

  it('prunePreviewsByDraft：正式 day 快照（轮询对账）已有条目的天候选整日让位', () => {
    let state = emptyPreviewState()
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    state = reduceItemPreviewEvent(state, previewFrame('run-1:2:1', 2, 1, '灵隐寺'))
    const draft = {
      dayList: [
        { dayNo: 1, items: [{ itemType: 'attraction', poiName: '宽窄巷子（正式）' }] },
        { dayNo: 2, items: [] },
      ],
    } as unknown as ItineraryDetail
    state = prunePreviewsByDraft(state, draft)
    // 第 1 天正式快照已到：该天候选整日让位（快照权威，不逐条合并）；第 2 天候选保留
    expect(namesOf(state)).toEqual(['灵隐寺'])
    // 快照里没有任何已排天 → 状态原样（引用不变）
    const emptyDraft = { dayList: [{ dayNo: 1, items: [] }] } as unknown as ItineraryDetail
    expect(prunePreviewsByDraft(state, emptyDraft)).toBe(state)
  })

  it('candidatesForDay：按 dayNo 过滤且 itemOrdinal 升序稳定', () => {
    let state = emptyPreviewState()
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:2', 1, 2, '人民公园'))
    state = reduceItemPreviewEvent(state, previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    state = reduceItemPreviewEvent(state, previewFrame('run-1:2:1', 2, 1, '灵隐寺'))
    const dayOne = candidatesForDay(state.candidates, 1)
    expect(dayOne.map((candidate) => `${candidate.itemOrdinal}:${String(candidate.item.poiName)}`))
      .toEqual(['1:宽窄巷子', '2:人民公园'])
  })
})

// ===== 2. hook 级：mock 流事件序列驱动真 useHomePlanning =====

const planningInput = { city: '成都', days: 3, persons: 2, stayNights: 2, preferences: [] }

type WaitCapture = { options: WaitForItineraryOptions; resolve: (detail: ItineraryDetail) => void }

const day = (dayNo: number, pois: string[]) => ({
  dayId: dayNo,
  dayNo,
  generationStatus: pois.length ? 'SUCCEEDED' : 'PENDING',
  items: pois.map((poiName) => ({ itemType: 'attraction', poiName })),
})

const readyDetail = {
  id: 7, status: 2, city: '成都', days: 3, persons: 2,
  dayList: [day(1, ['宽窄巷子']), day(2, ['灵隐寺']), day(3, ['西湖'])],
  budgetList: [], totalAmount: 0,
} as unknown as ItineraryDetail

describe('useHomePlanning M5a/M5b 流消费（mock api 交互）', () => {
  /** hook 状态探针：把流消费结果投影成 JSON 供断言。 */
  function PlanningProbe() {
    const planning = useHomePlanning()
    return createElement('div', null,
      createElement('button', {
        type: 'button',
        className: 'probe-start',
        onClick: () => void planning.submit(planningInput),
      }, 'start'),
      createElement('output', { className: 'probe-state' }, JSON.stringify({
        status: planning.status,
        coreReady: planning.coreReady,
        previews: planning.previews.map((candidate) => `${candidate.dayNo}:${String(candidate.item.poiName)}`),
      })),
    )
  }

  const roots: Array<{ root: Root; container: HTMLDivElement }> = []
  let streamChannel: { handlers: StreamHandlers; signal: AbortSignal } | null = null
  let waitCapture: WaitCapture | null = null

  async function flush() {
    await act(async () => {})
  }

  async function mountProbe() {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    roots.push({ root, container })
    await act(async () => { root.render(createElement(PlanningProbe)) })
    await flush()
    return container
  }

  async function startGeneration(container: HTMLDivElement) {
    await act(async () => { (container.querySelector('.probe-start') as HTMLButtonElement).click() })
    await flush()
    expect(container.querySelector('.probe-state')?.textContent).toContain('"status":"planning"')
  }

  async function feed(event: ItineraryStreamEvent) {
    await act(async () => { streamChannel!.handlers.onEvent(event) })
  }

  const stateOf = (container: HTMLDivElement) =>
    JSON.parse(container.querySelector('.probe-state')!.textContent ?? '{}') as {
      status: string
      coreReady: boolean
      previews: string[]
    }

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    sessionStorage.clear()
    generateMock.mockReset(); generateMock.mockResolvedValue({ id: 7, status: 1 } as ItineraryDetail)
    getDetailMock.mockReset(); getDetailMock.mockResolvedValue(readyDetail)
    streamChannel = null
    waitCapture = null
    // 流通道：捕获 handlers，挂到 abort 才收口（真连接的生命周期形状）
    streamMock.mockReset(); streamMock.mockImplementation((_id, signal, handlers) => {
      streamChannel = { handlers, signal }
      return new Promise<void>((resolve) => { signal.addEventListener('abort', () => resolve()) })
    })
    // 轮询通道：捕获 onUpdate，由用例驱动「正式快照到达 / 完成收尾」
    waitForMock.mockReset(); waitForMock.mockImplementation((_id, options) =>
      new Promise<ItineraryDetail>((resolve) => {
        waitCapture = { options: options ?? {}, resolve }
      }),
    )
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  it('事件序列 preview→core_ready→day 替换→withdrawn→完成：候选随权威快照让位，完成清残余', async () => {
    const container = await mountProbe()
    await startGeneration(container)

    // 候选逐项亮出（重复 previewId 去重）
    await feed(previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    await feed(previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    await feed(previewFrame('run-1:2:1', 2, 1, '灵隐寺'))
    expect(stateOf(container).previews).toEqual(['1:宽窄巷子', '2:灵隐寺'])

    // M5b：core_ready 里程碑提示置位（非阻断）；stage_timing 透传不炸
    await feed({ type: 'core_ready', itineraryId: 7, data: { revision: 3, daysEmitted: 2 } })
    await feed({ type: 'stage_timing', itineraryId: 7, data: { stage: 'core_ready', elapsedMs: 900 } })
    expect(stateOf(container).coreReady).toBe(true)

    // 正式 day 快照（轮询对账到达，第 1 天已排）：该天候选整日让位，第 2 天保留
    await act(async () => {
      waitCapture!.options.onUpdate?.({
        ...readyDetail,
        status: 1,
        dayList: [day(1, ['宽窄巷子（正式）']), day(2, []), day(3, [])],
      })
    })
    expect(stateOf(container).previews).toEqual(['2:灵隐寺'])
    expect(stateOf(container).coreReady).toBe(true)

    // 淘汰候选显式撤回：按 previewId 移除，不静默变成另一地点
    await feed(withdrawFrame('run-1:2:1', 2, 1))
    expect(stateOf(container).previews).toEqual([])

    // 完成收尾（waitForItinerary 返回 status=2）：提示一并清，进 ready
    await act(async () => { waitCapture!.resolve(readyDetail) })
    await flush()
    const final = stateOf(container)
    expect(final.status).toBe('ready')
    expect(final.coreReady).toBe(false)
    expect(final.previews).toEqual([])
  })

  it('SSE 终态帧（complete/done）也清残余预览与 core_ready 提示', async () => {
    const container = await mountProbe()
    await startGeneration(container)
    await feed(previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    await feed({ type: 'core_ready', itineraryId: 7, data: { revision: 3, daysEmitted: 1 } })
    expect(stateOf(container).coreReady).toBe(true)
    // 业务面直发终态帧（complete）先于轮询到达
    await feed({ type: 'complete', itineraryId: 7, data: { status: 'COMPLETED', dayCount: 3, degradedDays: [], versionId: 1 } })
    expect(stateOf(container)).toMatchObject({ coreReady: false, previews: [] })
  })

  it('换 run（regenerate 通道）：旧 run 预览作废、新 run 预览生效，旧 run 迟到帧忽略', async () => {
    const container = await mountProbe()
    await startGeneration(container)
    await feed(previewFrame('run-1:1:1', 1, 1, '宽窄巷子'))
    await feed(previewFrame('run-2:2:1', 2, 1, '灵隐寺', 'run-2'))
    expect(stateOf(container).previews).toEqual(['2:灵隐寺'])
    await feed(previewFrame('run-1:3:1', 3, 1, '旧 run 迟到帧', 'run-1'))
    expect(stateOf(container).previews).toEqual(['2:灵隐寺'])
  })
})

// ===== 3. 静态渲染：候选徽标 / core_ready 提示条 / 预览板候选行 =====

const fakeChat = (overrides: Partial<IntakeChat>): IntakeChat =>
  ({
    messages: [GREETING],
    slots: { city: '成都', days: 3, persons: 2 },
    firstMessage: '',
    ready: true,
    needsReconfirm: false,
    sending: false,
    error: '',
    needsLogin: false,
    send: async () => false,
    updateSlots: () => undefined,
    reconfirm: async () => false,
    reset: () => undefined,
    ...overrides,
  }) as IntakeChat

type Planning = ReturnType<typeof useHomePlanning>

const planning = (overrides: Partial<Planning>): Planning =>
  ({ status: 'idle', message: '', progress: 0, draft: null, previews: [], coreReady: false, busy: false, submit: async () => undefined, reset: () => undefined, ...overrides }) as Planning

const boardDraft = {
  id: 7, city: '成都', days: 1, persons: 2, startDate: null,
  dayList: [day(1, [])],
  budgetList: [], totalAmount: 0,
} as unknown as ItineraryDetail

describe('M5a/M5b 渲染（静态断言）', () => {
  it('生成中天卡：候选条目带「正在完善」徽标就地亮出，替代纯骨架 shimmer', () => {
    const draft = { id: 7, days: 1, dayList: [day(1, [])] } as unknown as ItineraryDetail
    const html = renderToStaticMarkup(createElement(TripPanel, {
      planning: planning({
        status: 'planning', busy: true, message: '正在安排第 1 天', progress: 2, draft,
        previews: [{
          previewId: 'run-1:1:1', runId: 'run-1', dayNo: 1, itemOrdinal: 1,
          item: { itemType: 'attraction', poiName: '宽窄巷子' },
        }],
      }),
      chat: fakeChat({}),
      onStart: () => undefined,
    }))
    expect(html).toContain('trip-preview-candidate')
    expect(html).toContain('宽窄巷子')
    expect(html).toContain('正在完善')
    // 有真候选就不再给纯骨架占位
    expect(html).not.toContain('shimmer-line')
  })

  it('core_ready 轻提示条：非阻断 status 文案「主行程已可查看，备选仍在完善」', () => {
    const draft = { id: 7, days: 1, dayList: [day(1, [])] } as unknown as ItineraryDetail
    const html = renderToStaticMarkup(createElement(TripPanel, {
      planning: planning({ status: 'planning', busy: true, draft, coreReady: true }),
      chat: fakeChat({}),
      onStart: () => undefined,
    }))
    expect(html).toContain('planning-core-hint')
    expect(html).toContain('主行程已可查看，备选仍在完善')
    // 未收到 core_ready 时不出现
    const without = renderToStaticMarkup(createElement(TripPanel, {
      planning: planning({ status: 'planning', busy: true, draft }),
      chat: fakeChat({}),
      onStart: () => undefined,
    }))
    expect(without).not.toContain('planning-core-hint')
  })

  it('TripBoard：候选按 dayNo 追加进对应天（空天出候选行，不落空态文案）', () => {
    const html = renderToStaticMarkup(createElement(TripBoard, {
      draft: boardDraft,
      previews: [{
        previewId: 'run-1:1:2', runId: 'run-1', dayNo: 1, itemOrdinal: 2,
        item: { itemType: 'food', poiName: '人民公园食堂' },
      }],
    }))
    expect(html).toContain('day-item is-preview')
    expect(html).toContain('人民公园食堂')
    expect(html).toContain('正在完善')
    expect(html).not.toContain('board-day-empty')
  })

  it('TripBoard：正式条目在前、候选行追加在后；无候选时与既有呈现一致', () => {
    const draft = {
      id: 7, city: '成都', days: 1, persons: 2, startDate: null,
      dayList: [day(1, ['宽窄巷子'])],
      budgetList: [], totalAmount: 0,
    } as unknown as ItineraryDetail
    const render = (previews?: ItemPreviewCandidate[]) =>
      renderToStaticMarkup(createElement(TripBoard, { draft, previews }))
    const withCandidate = render([{
      previewId: 'run-1:1:2', runId: 'run-1', dayNo: 1, itemOrdinal: 2,
      item: { poiName: '候选地点' },
    }])
    expect(withCandidate).toContain('宽窄巷子')
    expect(withCandidate).toContain('候选地点')
    // 候选缺 poiName 走兜底文案；itemType 缺键兜底「安排」
    expect(withCandidate).toContain('安排')
    expect(render()).not.toContain('is-preview')
  })
})

// ===== 4. M6 attempt 幂等（spec §12 创建入口）：key 持久化 / 复用 / 换新 =====

const ATTEMPT_KEY = 'sinan-intake-attempt'

const inputA = { city: '成都', days: 3, persons: 2, stayNights: 2, preferences: [] }
const inputB = { city: '杭州', days: 2, persons: 2, stayNights: 2, preferences: [] }

type PostCapture = { key: string; attemptAtPost: string | null }

interface StoredAttempt {
  attemptId: string
  hash: string
  phase: 'pending' | 'submitted'
  itineraryId: number | null
}

const storedAttempt = (): StoredAttempt | null => {
  const raw = sessionStorage.getItem(ATTEMPT_KEY)
  return raw ? (JSON.parse(raw) as StoredAttempt) : null
}

describe('useHomePlanning M6 attempt 幂等（spec §12 创建入口）', () => {
  function AttemptProbe() {
    const planning = useHomePlanning()
    return createElement('div', null,
      createElement('button', { type: 'button', className: 'probe-start-a', onClick: () => void planning.submit(inputA) }, 'a'),
      createElement('button', { type: 'button', className: 'probe-start-b', onClick: () => void planning.submit(inputB) }, 'b'),
      createElement('button', { type: 'button', className: 'probe-reset', onClick: () => planning.reset() }, 'reset'),
      createElement('output', { className: 'probe-state' }, JSON.stringify({ status: planning.status, message: planning.message })),
    )
  }

  const idemRoots: Array<{ root: Root; container: HTMLDivElement }> = []
  let posts: PostCapture[] = []
  let waitCapture: { resolve: (detail: ItineraryDetail) => void } | null = null

  async function flush() {
    await act(async () => {})
  }

  /** 排队一次「先捕获再失败」的 POST（rejected 的 Once 实现不走默认捕获体） */
  function failNextGenerate() {
    generateMock.mockImplementationOnce(async (_input, key) => {
      posts.push({ key: String(key), attemptAtPost: sessionStorage.getItem(ATTEMPT_KEY) })
      throw new TypeError('network down')
    })
  }

  /** 让在途 monitor 收尾（轮询通道返回 readyDetail）：解锁下一次 submit（busy 闸） */
  async function settleWait() {
    await act(async () => { waitCapture!.resolve(readyDetail) })
    await flush()
  }

  async function mount() {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    idemRoots.push({ root, container })
    await act(async () => { root.render(createElement(AttemptProbe)) })
    await flush()
    return container
  }

  async function click(container: HTMLDivElement, className: string) {
    await act(async () => { (container.querySelector(className) as HTMLButtonElement).click() })
    await flush()
  }

  const stateOf = (container: HTMLDivElement) =>
    JSON.parse(container.querySelector('.probe-state')!.textContent ?? '{}') as { status: string; message: string }

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    sessionStorage.clear()
    posts = []
    waitCapture = null
    // 捕获 POST 时点的 key 与 sessionStorage 状态（「先落盘再 POST」的直接证据）
    generateMock.mockReset(); generateMock.mockImplementation(async (_input, key) => {
      posts.push({ key: String(key), attemptAtPost: sessionStorage.getItem(ATTEMPT_KEY) })
      return { id: 7, status: 1 } as ItineraryDetail
    })
    getDetailMock.mockReset(); getDetailMock.mockResolvedValue(readyDetail)
    // 流立即收口：monitor 的 `await sse` 不悬挂，submit 能退净（request.current 清空），
    // 同一容器内连续多次 submit 才可达；等待收尾由 waitForMock/settleWait 控制
    streamMock.mockReset(); streamMock.mockImplementation(async () => {})
    waitForMock.mockReset(); waitForMock.mockImplementation(() =>
      new Promise<ItineraryDetail>((resolve) => { waitCapture = { resolve } }))
  })

  afterEach(async () => {
    vi.restoreAllMocks()
    for (const { root, container } of idemRoots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    sessionStorage.clear()
  })

  it('首次 POST 前已持久化 attemptId+hash+状态；成功后 phase=submitted 且绑 itineraryId', async () => {
    const container = await mount()
    await click(container, '.probe-start-a')
    expect(posts).toHaveLength(1)
    // POST 执行时点记录已在（phase 还是 pending）——落盘先于发请求
    const atPost = JSON.parse(posts[0].attemptAtPost!) as StoredAttempt
    expect(atPost.attemptId).toBe(posts[0].key)
    expect(atPost.attemptId).toBeTruthy()
    expect(atPost.hash).toBeTypeOf('string')
    expect(atPost.phase).toBe('pending')
    // 成功后绑定 itineraryId（此后恢复只轮询这一条）
    expect(storedAttempt()).toMatchObject({ attemptId: posts[0].key, phase: 'submitted', itineraryId: 7 })
    expect(stateOf(container).status).toBe('planning')
  })

  it('网络失败后的重试复用同一 key（E13：创建已接受丢响应仍只有一条行程）', async () => {
    failNextGenerate()
    const container = await mount()
    await click(container, '.probe-start-a')
    expect(posts).toHaveLength(1)
    expect(stateOf(container).status).toBe('error')
    // 记录保留（结果未知）：同输入再试拿同一 key，后端同键回放同一行程
    await click(container, '.probe-start-a')
    expect(posts).toHaveLength(2)
    expect(posts[1].key).toBe(posts[0].key)
    expect(stateOf(container).status).toBe('planning')
  })

  it('请求实质变化换新 key；reset（明确另建）后同输入也换新 key', async () => {
    const container = await mount()
    await click(container, '.probe-start-a')
    expect(posts[0].key).toBeTruthy()
    await settleWait()
    await click(container, '.probe-start-b')
    expect(posts[1].key).not.toBe(posts[0].key)
    await settleWait()
    // reset 清 attempt 记录：同样的输入也是一次显式的新行程
    await click(container, '.probe-reset')
    expect(storedAttempt()).toBeNull()
    await click(container, '.probe-start-a')
    expect(posts[2].key).not.toBe(posts[0].key)
  })

  it('storage 写不进（隐私模式/配额满）：降级内存影子，会话内重试仍复用同一 key', async () => {
    const setItemSpy = vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    failNextGenerate()
    const container = await mount()
    await click(container, '.probe-start-a')
    expect(posts).toHaveLength(1)
    expect(setItemSpy).toHaveBeenCalled()
    await click(container, '.probe-start-a')
    expect(posts).toHaveLength(2)
    expect(posts[1].key).toBe(posts[0].key)
    expect(stateOf(container).status).toBe('planning')
  })

  it('已有 itineraryId 的刷新恢复只轮询不重复提交（attempt 记录在场也不触发 POST）', async () => {
    sessionStorage.setItem('sinan-intake-generation', '7')
    sessionStorage.setItem(ATTEMPT_KEY, JSON.stringify({
      attemptId: 'seed-key', hash: 'h', phase: 'submitted', itineraryId: 7,
    }))
    await mount()
    await flush()
    expect(generateMock).not.toHaveBeenCalled()
    expect(getDetailMock).toHaveBeenCalledWith(7)
  })
})
