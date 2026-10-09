import { act, StrictMode } from 'react'
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clarifyItinerary, generateItinerary, getItineraryDetail, ReactApiError, streamItineraryEvents, waitForItinerary } from '../../api/sinan'
import type { ItineraryDetail } from '../../types/itinerary'
import type { ClarifyResponse, IntakeState } from '../../types/generated/contracts'
import type { useHomePlanning } from './useHomePlanning'
import { useIntakeChat } from './useIntakeChat'
import type { IntakeChat } from './useIntakeChat'
import { GREETING, saveIntake } from './intakeSlots'
import { ChatIntake } from './ChatIntake'
import { HomeStudio } from './HomeStudio'
import { TripBoard } from './TripBoard'
import { TripPanel } from './TripPanel'

/**
 * 对话壳与实时预览测试：主体是静态渲染断言（同 TripBadges.test.ts 纪律：零新依赖、
 * renderToStaticMarkup 断言结构，不跑 effect、不依赖网络/路由）；
 * 「重新说」相位锁是纯 effect 行为，静态渲染够不着，另有 mock api 层的
 * 交互块（真 createRoot + act，同 llmGateway.test.ts 纪律）。
 */

vi.mock('../../api/sinan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sinan')>()
  return {
    ...actual,
    clarifyItinerary: vi.fn(),
    generateItinerary: vi.fn(),
    getItineraryDetail: vi.fn(),
    waitForItinerary: vi.fn(),
    streamItineraryEvents: vi.fn(),
  }
})

const clarifyMock = vi.mocked(clarifyItinerary)
const generateMock = vi.mocked(generateItinerary)
const getDetailMock = vi.mocked(getItineraryDetail)
const waitForMock = vi.mocked(waitForItinerary)
const streamMock = vi.mocked(streamItineraryEvents)

type Planning = ReturnType<typeof useHomePlanning>

const planning = (overrides: Partial<Planning>): Planning =>
  ({ status: 'idle', message: '', progress: 0, draft: null, previews: [], coreReady: false, busy: false, submit: async () => undefined, reset: () => undefined, ...overrides }) as Planning

const fakeChat = (overrides: Partial<IntakeChat>): IntakeChat =>
  ({
    messages: [GREETING],
    slots: {},
    firstMessage: '',
    ready: false,
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

const day = (dayNo: number, pois: string[]) => ({
  dayId: dayNo,
  dayNo,
  generationStatus: pois.length ? 'SUCCEEDED' : 'PENDING',
  items: pois.map((poiName) => ({ itemType: 'attraction', poiName })),
})

describe('HomeStudio（首页 phase 状态机）', () => {
  afterEach(() => sessionStorage.removeItem('sinan-intake-v1'))
  it('idle：首屏只有一张居中对话卡，右栏不上屏', () => {
    const html = renderToStaticMarkup(createElement(HomeStudio, { query: new URLSearchParams() }))
    expect(html).toContain('home-studio is-idle')
    expect(html).toContain('intake-centered')
    expect(html).toContain('说一句话，行程就有了')
    expect(html).toContain('intake-chips')
    expect(html).not.toContain('trip-preview')
  })
  it('有对话历史即 active 双栏（sessionStorage 恢复同理）：右栏出收集进度', () => {
    // M2 存储带版本号：走 saveIntake 统一写入（手写 JSON 缺 v 字段会被 loadIntake 拒收）
    saveIntake({
      messages: [{ id: 'greeting', role: 'assistant', text: 'hi' }, { id: 'u1', role: 'user', text: '想去成都' }],
      slots: { city: '成都' },
      firstMessage: '想去成都',
    })
    const html = renderToStaticMarkup(createElement(HomeStudio, { query: new URLSearchParams() }))
    expect(html).toContain('home-studio is-active')
    expect(html).toContain('home-studio-panel')
    expect(html).toContain('intake-dock')
    expect(html).toContain('slot-checklist')
    expect(html).toContain('还差 2 项就能开工')
  })
  it('idle 态若存在草稿记录，渲染恢复条提示与对话记录入口', () => {
    localStorage.setItem(
      'sinan-intake-sessions-v1',
      JSON.stringify([
        {
          id: 'draft-1',
          title: '成都 · 4天 · 2人',
          updatedAt: Date.now() - 60000,
          messages: [{ id: '1', role: 'user', text: '想去成都玩4天' }],
          slots: { city: '成都', days: 4, persons: 2 },
          firstMessage: '想去成都玩4天',
        },
      ]),
    )
    const html = renderToStaticMarkup(createElement(HomeStudio, { query: new URLSearchParams() }))
    expect(html).toContain('intake-resume-bar')
    expect(html).toContain('成都 · 4天 · 2人')
    expect(html).toContain('继续上次对话')
    expect(html).toContain('对话记录 (1)')
    localStorage.removeItem('sinan-intake-sessions-v1')
  })
})

describe('TripPanel（active 右栏容器）', () => {
  const panel = (planningOverrides: Partial<Planning>, chatOverrides: Partial<IntakeChat>) =>
    renderToStaticMarkup(createElement(TripPanel, {
      planning: planning(planningOverrides),
      chat: fakeChat(chatOverrides),
      onStart: () => undefined,
    }))

  it('collecting：stepper 当前步=补信息，收集清单出缺口提示与下一项高亮', () => {
    const html = panel({}, { slots: { city: '成都' } })
    expect(html).toContain('trip-steps')
    expect(html).toContain('aria-current="step"')
    expect(html).toContain('role="status"')
    expect(html).toContain('还差 2 项就能开工')
    expect(html).toContain('成都')
    expect(html).toContain('is-next')
    expect(html).not.toContain('trip-confirm')
  })
  it('confirm：右栏呈现出发前确认表单（IntakeConfirm 承接开工契约 .intake-start）', () => {
    const html = panel({}, { ready: true, slots: { city: '成都', days: 4, persons: 2, budget: 3000 } })
    expect(html).toContain('trip-confirm')
    expect(html).toContain('intake-confirm')
    expect(html).toContain('出发前确认')
    expect(html).toContain('成都')
    expect(html).toContain('intake-start')
    expect(html).toContain('就这样，开始规划')
  })
  it('confirm（needsReconfirm）：表单改动后留在确认卡，开工键切换为重验话术（M2）', () => {
    const html = panel({}, { ready: false, needsReconfirm: true, slots: { city: '成都', days: 4, persons: 2 } })
    expect(html).toContain('trip-confirm')
    expect(html).toContain('信息已更新，重新确认')
    expect(html).not.toContain('就这样，开始规划')
  })
  it('generating：四段阶段进度（SSE 文案）+ 逐日生长卡（原 TripPreview 逻辑卡4 迁入）', () => {
    const draft = {
      id: 7,
      days: 3,
      dayList: [day(1, ['宽窄巷子', '人民公园']), day(2, []), day(3, [])],
    } as unknown as ItineraryDetail
    const html = panel(
      { status: 'planning', busy: true, message: '正在安排第 2 天', progress: 2, draft },
      { ready: true, slots: { city: '成都', days: 2, persons: 2 } },
    )
    expect(html).toContain('planning-stages')
    expect(html).toContain('核对行程')
    expect(html).toContain('正在安排第 2 天')
    expect(html).toContain('宽窄巷子')
    expect(html).toContain('安排中…')
    expect(html).toContain('第 3 天')
    expect(html).toContain('trip-preview-days')
    expect(html).toContain('生成中')
  })
  it('error 兜底：提示可回对话重开，stepper 回到「确认」步', () => {
    const html = panel({ status: 'error', message: '生成失败' }, { ready: true })
    expect(html).toContain('planning-retry-hint')
    expect(html).toContain('开始规划')
    expect(html).not.toContain('trip-confirm')
  })
  it('login 兜底：给「登录并继续」入口', () => {
    const html = panel({ status: 'login', message: '登录后即可开始规划，你填写的想法会保留。' }, { ready: true })
    expect(html).toContain('登录并继续')
    expect(html).not.toContain('trip-confirm')
  })
  it('pending 兜底：保留已排好的天卡', () => {
    const draft = { id: 7, days: 1, dayList: [day(1, ['西湖'])] } as unknown as ItineraryDetail
    const html = panel({ status: 'pending', message: '进度暂时不可用，已保留当前行程。', draft }, { ready: true })
    expect(html).toContain('西湖')
    expect(html).not.toContain('planning-stages')
  })
  it('ready（done 态）：上 TripBoard 预览板，主 CTA href=/trips/:id（900ms 自动跳转已移除）', () => {
    const draft = { id: 7, days: 1, dayList: [day(1, ['西湖'])], budgetList: [] } as unknown as ItineraryDetail
    const html = panel({ status: 'ready', message: '你的行程已准备好', progress: 4, draft }, { ready: true })
    expect(html).toContain('trip-board')
    expect(html).toContain('trip-board-cta')
    expect(html).toContain('打开完整行程')
    expect(html).toContain('href="/trips/7"')
    expect(html).not.toContain('查看完整行程')
  })
})

describe('TripBoard（done 态只读预览板）', () => {
  const draft = {
    id: 7,
    city: '成都',
    days: 3,
    persons: 2,
    startDate: null,
    dayList: [day(1, []), day(2, ['宽窄巷子', '人民公园']), day(3, ['大熊猫基地'])],
    budgetList: [{ category: '餐饮', amount: 640, itemCount: 6 }],
    totalAmount: 2340,
  } as unknown as ItineraryDetail
  const board = () => renderToStaticMarkup(createElement(TripBoard, { draft }))

  it('契约类：根 .trip-board、主 CTA .trip-board-cta 且 href=/trips/:id、文本含「打开完整行程」', () => {
    const html = board()
    expect(html).toContain('trip-board')
    expect(html).toContain('trip-board-cta')
    expect(html).toContain('打开完整行程')
    expect(html).toContain('href="/trips/7"')
  })
  it('头部摘要「城市 · N 天 · M 人」+ 无出发日期显「日期待定」；预算小计一行', () => {
    const html = board()
    expect(html).toContain('成都')
    expect(html).toContain('3 天 · 2 人')
    expect(html).toContain('日期待定')
    expect(html).toContain('预算小计')
    expect(html).toContain('￥2340')
    expect(html).toContain('餐饮 ￥640')
  })
  it('DAY chips 默认选中第一个已完成的天（第 1 天空 → 落第 2 天）+ 当天时间线只读条目', () => {
    const html = board()
    expect(html).toContain('DAY 01')
    expect(html).toContain('宽窄巷子')
    expect(html).toContain('day-item-time')
    expect(html).toContain('item-type')
    expect(html).not.toContain('item-feedback')
    expect(html).not.toContain('item-meta')
  })
  it('地图不内嵌：只有轻入口文案，无 maplibre 容器', () => {
    const html = board()
    expect(html).toContain('地图与逐点编辑在完整行程里')
    expect(html).not.toContain('maplibre')
    expect(html).not.toContain('canvas')
  })
})

describe('ChatIntake（对话壳静态冒烟）', () => {
  /** 会话状态由宿主持有（HomeStudio 派生 idle/active），测试里用同一挂法供 hooks 初值。 */
  function ChatIntakeHarness({ query }: { query: URLSearchParams }) {
    const chat = useIntakeChat()
    return createElement(ChatIntake, { query, chat })
  }
  it('初始渲染出问候语、开场模板 chips 与输入框', () => {
    const html = renderToStaticMarkup(createElement(ChatIntakeHarness, { query: new URLSearchParams() }))
    expect(html).toContain('想去哪儿玩')
    expect(html).toContain('说说你的旅行想法')
    expect(html).toContain('intake-chips')
    expect(html).toContain('把成都走慢一点')
  })
  it('?city= 预填首句草稿', () => {
    const html = renderToStaticMarkup(createElement(ChatIntakeHarness, { query: new URLSearchParams('city=成都&days=3') }))
    expect(html).toContain('想去成都玩 3 天')
  })
})

/** 「重新说」会话重置 + done 刷新保真（mock api 层驱动真实状态机）：done 态点
 * 「重新说」须清对话、清生成态并清掉保留的 generationId（唯一清空点），停回 idle
 * 居中、新消息进会话才解锁回双栏；done 后「刷新」（卸载重挂）靠保留的 id 经
 * resume 恢复 ready+draft 呈现 TripBoard，不再落 confirm 可重复生成（F4）。 */
/** M1a 起服务端 clarify 响应携带完整规范化累计状态；测试 fixture 用空态 + 基础槽位。 */
function intakeState(base: Partial<Pick<IntakeState, 'city' | 'days' | 'persons'>> = {}): IntakeState {
  return {
    schemaVersion: 1,
    city: null,
    days: null,
    persons: null,
    budget: null,
    startDate: null,
    stayNights: null,
    preferences: [],
    hotelTier: null,
    originCity: null,
    requirements: {
      schemaVersion: 1,
      dayWindows: [],
      requiredPlaces: [],
      excludedPlaces: [],
      excludedCategories: [],
      pace: null,
      transportPreference: null,
      maxWalkMinutesPerLeg: null,
      budgetPolicy: null,
      lodging: null,
      unresolvedRequests: [],
    },
    optionalAsked: [],
    optionalPending: [],
    negotiations: {},
    acceptances: {},
    ...base,
  }
}

/** M2 起契约要求 reply/replyFor/next 必备：fixture 统一从工厂出，形状随生成类型走。 */
function clarifyFixture(overrides: Partial<ClarifyResponse> = {}): ClarifyResponse {
  return {
    slots: {},
    state: intakeState(),
    missing: [],
    question: null,
    ready: false,
    options: [],
    blocked: false,
    degraded: false,
    reply: null,
    replyFor: null,
    next: null,
    ...overrides,
  }
}

describe('HomeStudio「重新说」锁（mock api 交互）', () => {
  const clarifyReady = clarifyFixture({
    slots: { city: '成都', days: 4, persons: 2 },
    state: intakeState({ city: '成都', days: 4, persons: 2 }),
    missing: [],
    ready: true,
    reply: '成都 4 天 2 人的要素齐了，确认一下就开工。',
    replyFor: 'confirm',
    next: 'confirm',
  })
  const roots: Array<{ root: Root; container: HTMLDivElement }> = []

  /** 异步链（clarify/generate 的 promise 落状态）补空 act 窗口，避免 act 告警。 */
  async function flush() {
    await act(async () => {})
  }

  async function mountStudio() {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    const record = { root, container }
    roots.push(record)
    await act(async () => { root.render(createElement(HomeStudio, { query: new URLSearchParams() })) })
    await flush()
    return record
  }

  /** happy-dom 下写受控 input：走原型 value setter 触发 React 的 onChange。 */
  function setInput(input: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
    setter?.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }

  async function say(container: HTMLDivElement, text: string) {
    await act(async () => {
      setInput(container.querySelector('input[aria-label="说说你的旅行想法"]') as HTMLInputElement, text)
      ;(container.querySelector('.intake-composer button[type="submit"]') as HTMLButtonElement).click()
    })
    await flush()
  }

  const studioClass = (container: HTMLDivElement) => container.querySelector('.home-studio')?.className ?? ''

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    sessionStorage.clear()
    // happy-dom 的 WAAPI：useFlip 取消上一段动画会以未处理 AbortError 冒出（真浏览器
    // 不 await finished 就无此问题）。相位锁断言只看 class/结构，动画桩掉即可。
    vi.spyOn(Element.prototype, 'animate').mockReturnValue({ cancel: () => {} } as unknown as Animation)
    clarifyMock.mockReset(); clarifyMock.mockResolvedValue(clarifyReady)
    generateMock.mockReset(); generateMock.mockResolvedValue({ id: 7 } as unknown as ItineraryDetail)
    // draft.days>0 是 TripPanel 上预览板（TripBoard）的门槛，得给足形状
    waitForMock.mockReset(); waitForMock.mockResolvedValue({
      id: 7, status: 2, city: '成都', days: 1, persons: 2,
      dayList: [day(1, ['西湖'])], budgetList: [], totalAmount: 0,
    } as unknown as ItineraryDetail)
    // 刷新恢复通道（resume 拉详情）默认返回已完成行程
    getDetailMock.mockReset(); getDetailMock.mockResolvedValue({
      id: 7, status: 2, city: '成都', days: 1, persons: 2,
      dayList: [day(1, ['西湖'])], budgetList: [], totalAmount: 0,
    } as unknown as ItineraryDetail)
    streamMock.mockReset(); streamMock.mockResolvedValue(undefined)
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  it('done 态点「重新说」：对话与生成态一并归零、generationId 清空，停回 idle 居中；再开聊才解锁', async () => {
    const { container } = await mountStudio()
    expect(studioClass(container)).toContain('is-idle')

    // 先走到 done：clarify 齐槽 → 确认条开工 → 生成完成（双栏 + 预览板）
    await say(container, '国庆想去成都玩 4 天，两个人')
    expect(container.querySelector('.intake-confirm')).not.toBeNull()
    await act(async () => { (container.querySelector('.intake-start') as HTMLButtonElement).click() })
    await flush()
    expect(studioClass(container)).toContain('is-active')
    expect(container.querySelector('.trip-board')).not.toBeNull()
    // done 态保留 generationId（F4）：刷新后 resume 靠它恢复 ready
    expect(sessionStorage.getItem('sinan-intake-generation')).toBe('7')

    // 「重新说」：会话重置是 id 的唯一清空点，生成态与草稿一并归零，回 idle 居中
    await act(async () => { (container.querySelector('.trip-board-reset') as HTMLButtonElement).click() })
    expect(studioClass(container)).toContain('is-idle')
    expect(container.querySelector('.home-studio-panel')).toBeNull()
    expect(sessionStorage.getItem('sinan-intake-generation')).toBeNull()

    // 锁只活一次：再次开聊（新消息进会话）即恢复 active 双栏
    await say(container, '那改去杭州，3 天')
    expect(studioClass(container)).toContain('is-active')
    expect(container.querySelector('.home-studio-panel')).not.toBeNull()
    expect(clarifyMock).toHaveBeenCalledTimes(2)
  })

  it('active 对话态下点击「返回首页」（或派发 sinan:reset-home）：即刻退回 idle 居中，解锁展示区', async () => {
    const { container } = await mountStudio()
    await say(container, '想去成都玩 3 天')
    expect(studioClass(container)).toContain('is-active')
    expect(container.querySelector('.intake-reset-btn')).not.toBeNull()

    // 点击左栏顶部的「返回首页」按钮
    await act(async () => { (container.querySelector('.intake-reset-btn') as HTMLButtonElement).click() })
    await flush()
    expect(studioClass(container)).toContain('is-idle')
    expect(container.querySelector('.home-studio-panel')).toBeNull()
    expect(container.querySelector('.home-showcase')).not.toBeNull()
  })

  it('done 态「刷新」（卸载重挂）：保留的 generationId 经 resume 恢复 ready+TripBoard，不落 confirm（F4）', async () => {
    const first = await mountStudio()
    await say(first.container, '国庆想去成都玩 4 天，两个人')
    await act(async () => { (first.container.querySelector('.intake-start') as HTMLButtonElement).click() })
    await flush()
    expect(first.container.querySelector('.trip-board')).not.toBeNull()
    expect(sessionStorage.getItem('sinan-intake-generation')).toBe('7')

    // 模拟刷新：整树卸载后全新挂载（sessionStorage 同页保留——对话历史与 id 都在）
    await act(async () => { first.root.unmount() })
    first.container.remove()
    roots.splice(roots.indexOf(first), 1)

    const second = await mountStudio()
    // 会话历史在，刷新落地即 active 双栏；resume 拉到 status=2 详情 → ready+draft
    expect(studioClass(second.container)).toContain('is-active')
    await flush()
    expect(studioClass(second.container)).toContain('is-active')
    // 右栏是 TripBoard 预览板，不是 confirm 摘要+快捷开工（F4 病灶不再现）
    expect(second.container.querySelector('.trip-board')).not.toBeNull()
    expect(second.container.querySelector('.trip-confirm')).toBeNull()
    expect(getDetailMock).toHaveBeenCalledTimes(1)
    // id 仍保留：再次刷新仍能恢复；清空只发生在「重新说」
    expect(sessionStorage.getItem('sinan-intake-generation')).toBe('7')
  })
})

/** F5 登录续发（PLAN 2026-10-03 §2.3）：匿名撞 clarify 401 时那句话暂存 sessionStorage
 * （sinan-intake-pending），登录回跳重新挂载且已登录（localStorage 有 sinan-username）
 * → 先清键再自动补发一次，未登录保留键。挂载行为静态渲染够不着，mock api 层真挂载
 * 驱动（同「重新说」锁块纪律）。 */
describe('useIntakeChat 登录续发（F5 mock api 交互）', () => {
  const PENDING_KEY = 'sinan-intake-pending'
  const SENT_TEXT = '国庆想去成都玩 4 天，两个人'
  const clarifyAskDays = clarifyFixture({
    slots: { city: '成都' },
    state: intakeState({ city: '成都' }),
    missing: ['days', 'persons'],
    question: '玩几天？',
    options: ['3 天'],
    replyFor: 'days',
    next: 'days',
  })

  function IntakeHarness() {
    const chat = useIntakeChat()
    return createElement('div', null,
      createElement('button', { type: 'button', onClick: () => void chat.send(SENT_TEXT) }, 'send'),
      createElement('ul', null, chat.messages.map((msg) => createElement('li', { key: msg.id }, msg.text))),
      chat.needsLogin ? createElement('em', null, 'needs-login') : null,
    )
  }

  const roots: Array<{ root: Root; container: HTMLDivElement }> = []

  async function mountHarness(strict = false) {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    roots.push({ root, container })
    const node = strict ? createElement(StrictMode, null, createElement(IntakeHarness)) : createElement(IntakeHarness)
    await act(async () => { root.render(node) })
    return container
  }

  /** 挂载续发走 setTimeout(0)（见 useIntakeChat 内注释：躲开 StrictMode 模拟卸载对
   * 挂载期请求的 abort 误杀），真定时器等一拍让它落地。 */
  async function flushAutoSend() {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
  }

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    sessionStorage.clear()
    localStorage.clear()
    clarifyMock.mockReset(); clarifyMock.mockResolvedValue(clarifyAskDays)
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    sessionStorage.clear()
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('匿名撞 401：needsLogin 置位，那句待发文本暂存进 pending 键', async () => {
    clarifyMock.mockRejectedValue(new ReactApiError('请求失败（401）', 401))
    const container = await mountHarness()
    await act(async () => { (container.querySelector('button') as HTMLButtonElement).click() })
    await act(async () => {})
    expect(container.querySelector('em')?.textContent).toBe('needs-login')
    expect(sessionStorage.getItem(PENDING_KEY)).toBe(SENT_TEXT)
  })

  it('已登录 + pending 键：挂载自动补发一次、键被清；StrictMode 双挂载不双发', async () => {
    sessionStorage.setItem(PENDING_KEY, SENT_TEXT)
    localStorage.setItem('sinan-username', '盛楠')
    const container = await mountHarness(true)
    await flushAutoSend()
    expect(clarifyMock).toHaveBeenCalledTimes(1)
    expect(clarifyMock.mock.calls[0][0]).toBe(SENT_TEXT)
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull()
    // 补发走现有 send 通道：用户消息进会话、追问回复照常追加
    expect(container.textContent).toContain(SENT_TEXT)
    expect(container.textContent).toContain('玩几天？')
  })

  it('有 pending 键但未登录：不补发，键保留等下次', async () => {
    sessionStorage.setItem(PENDING_KEY, SENT_TEXT)
    await mountHarness()
    await flushAutoSend()
    expect(clarifyMock).not.toHaveBeenCalled()
    expect(sessionStorage.getItem(PENDING_KEY)).toBe(SENT_TEXT)
  })

  it('键已清后再挂载不补发（幂等闸的直接断言）', async () => {
    sessionStorage.setItem(PENDING_KEY, SENT_TEXT)
    localStorage.setItem('sinan-username', '盛楠')
    await mountHarness()
    await flushAutoSend()
    expect(clarifyMock).toHaveBeenCalledTimes(1)
    // 第二次挂载（键已被第一轮清掉）：以「清键」为幂等闸，不许再发
    await mountHarness()
    await flushAutoSend()
    expect(clarifyMock).toHaveBeenCalledTimes(1)
  })
})

/** M2 后端就绪权威（spec §7.2）：ready 只信 clarify 响应的 ready && !blocked；
 * 初始化/恢复会话不落 ready；表单编辑后到达的迟到响应整体丢弃；blocked 硬阻断
 * 任何路径解不了锁。hook 级行为用 probe 组件真挂载驱动（同前两块 mock api 交互纪律）。 */
describe('useIntakeChat 后端就绪权威与迟到守卫（M2 mock api 交互）', () => {
  const completeSlots = { city: '成都', days: 4, persons: 2 }

  /** hook 状态探针：把就绪/重验/槽位投影成 JSON，供断言读取。 */
  function ChatProbe({ text }: { text: string }) {
    const chat = useIntakeChat()
    return createElement('div', null,
      createElement('button', { type: 'button', className: 'probe-send', onClick: () => void chat.send(text) }, 'send'),
      createElement('button', { type: 'button', className: 'probe-edit', onClick: () => chat.updateSlots({ days: 2 }) }, 'edit'),
      createElement('output', { className: 'probe-state' }, JSON.stringify({
        ready: chat.ready,
        needsReconfirm: chat.needsReconfirm,
        city: chat.slots.city ?? null,
        days: chat.slots.days ?? null,
        msgs: chat.messages.length,
      })),
    )
  }

  const roots: Array<{ root: Root; container: HTMLDivElement }> = []

  async function mountProbe(text: string) {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    roots.push({ root, container })
    await act(async () => { root.render(createElement(ChatProbe, { text })) })
    return container
  }

  const stateOf = (container: HTMLDivElement) =>
    JSON.parse(container.querySelector('.probe-state')!.textContent ?? '{}') as {
      ready: boolean
      needsReconfirm: boolean
      city: string | null
      days: number | null
      msgs: number
    }

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    sessionStorage.clear()
    clarifyMock.mockReset()
    generateMock.mockReset(); generateMock.mockResolvedValue({ id: 7 } as unknown as ItineraryDetail)
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  it('① ready 只信后端：恢复的会话槽位再齐也不落 ready，须本轮响应 ready&&!blocked', async () => {
    // 版本合法的历史会话、必填槽位已齐——旧 slotsReady 推导会直接解锁，M2 一律先视为未就绪
    saveIntake({
      messages: [GREETING, { id: 'u1', role: 'user', text: '想去成都玩 4 天，两个人' }],
      slots: completeSlots,
      firstMessage: '想去成都玩 4 天，两个人',
    })
    const container = await mountProbe('那就 10 月 1 号出发')
    expect(stateOf(container)).toMatchObject({ ready: false, needsReconfirm: false })

    // 本轮响应后端仍说不就绪：ready=false 不得解锁（哪怕槽位已齐）
    clarifyMock.mockResolvedValue(clarifyFixture({
      slots: completeSlots,
      state: intakeState(completeSlots),
      ready: false,
      question: '大概哪天出发？',
      replyFor: 'start_date',
      next: 'start_date',
    }))
    await act(async () => { (container.querySelector('.probe-send') as HTMLButtonElement).click() })
    await act(async () => {})
    expect(stateOf(container).ready).toBe(false)
  })

  it('③ 迟到响应不覆盖新修改：表单编辑后到达的 clarify 响应整体丢弃', async () => {
    let resolveLater!: (value: ClarifyResponse) => void
    clarifyMock.mockImplementationOnce(() => new Promise<ClarifyResponse>((resolve) => { resolveLater = resolve }))
    const container = await mountProbe('想去成都')
    await act(async () => { (container.querySelector('.probe-send') as HTMLButtonElement).click() })
    // 响应未回时用户改了表单：seq 自增，在途响应作废
    await act(async () => { (container.querySelector('.probe-edit') as HTMLButtonElement).click() })
    expect(stateOf(container)).toMatchObject({ ready: false, needsReconfirm: true, days: 2 })
    // 迟到响应带着齐槽 + ready=true 回来了：slots/ready/needsReconfirm 不被覆盖，消息不追加
    await act(async () => {
      resolveLater(clarifyFixture({
        slots: completeSlots,
        state: intakeState(completeSlots),
        ready: true,
        reply: '齐了，确认一下就开工。',
        replyFor: 'confirm',
        next: 'confirm',
      }))
    })
    expect(stateOf(container)).toMatchObject({ ready: false, needsReconfirm: true, city: null, days: 2, msgs: 2 })
    expect(clarifyMock).toHaveBeenCalledTimes(1)
  })

  it('④ blocked 硬阻断：即便响应带 ready=true，blocked=true 也不解锁', async () => {
    clarifyMock.mockResolvedValue(clarifyFixture({
      slots: completeSlots,
      state: intakeState(completeSlots),
      ready: true,
      blocked: true,
      reply: '单程最多排 7 天，改一下天数再出发。',
      replyFor: 'days_limit',
      next: 'days_limit',
    }))
    const container = await mountProbe('想去成都玩 10 天')
    await act(async () => { (container.querySelector('.probe-send') as HTMLButtonElement).click() })
    await act(async () => {})
    expect(stateOf(container).ready).toBe(false)
    // 未就绪就不会进生成：生成接口一次都没被打
    expect(generateMock).not.toHaveBeenCalled()
  })
})

/** M2 表单重验全链路（spec §7.2「内容改动后就绪失效，经过后端重验才允许生成」）：
 * 确认卡改表单 → ready 失效 + 待重验（开工键切换重验话术）→ 点开工先打一轮
 * clarify 重验，ready 恢复才真正 submit 生成；重验不过不触发生成、留在待重验态。 */
describe('M2 表单重验（mock api 交互）', () => {
  const clarifyReady = clarifyFixture({
    slots: { city: '成都', days: 4, persons: 2 },
    state: intakeState({ city: '成都', days: 4, persons: 2 }),
    ready: true,
    reply: '要素齐了，确认一下就开工。',
    replyFor: 'confirm',
    next: 'confirm',
  })
  const clarifyBudgetLow = clarifyFixture({
    slots: { city: '成都', days: 4, persons: 2 },
    state: intakeState({ city: '成都', days: 4, persons: 2 }),
    ready: false,
    question: '预算有点紧：人均每天不到 100，接受按估算排吗？',
    replyFor: 'budget_low',
    next: 'budget_low',
  })

  const roots: Array<{ root: Root; container: HTMLDivElement }> = []

  /** 异步链（clarify/generate 的 promise 落状态）补空 act 窗口，避免 act 告警。 */
  async function flush() {
    await act(async () => {})
  }

  async function mountStudio() {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    roots.push({ root, container })
    await act(async () => { root.render(createElement(HomeStudio, { query: new URLSearchParams() })) })
    await flush()
    return { root, container }
  }

  /** happy-dom 下写受控 input：走原型 value setter 触发 React 的 onChange。 */
  function setInput(input: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
    setter?.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  }

  async function say(container: HTMLDivElement, text: string) {
    await act(async () => {
      setInput(container.querySelector('input[aria-label="说说你的旅行想法"]') as HTMLInputElement, text)
      ;(container.querySelector('.intake-composer button[type="submit"]') as HTMLButtonElement).click()
    })
    await flush()
  }

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    sessionStorage.clear()
    vi.spyOn(Element.prototype, 'animate').mockReturnValue({ cancel: () => {} } as unknown as Animation)
    clarifyMock.mockReset()
    generateMock.mockReset(); generateMock.mockResolvedValue({ id: 7 } as unknown as ItineraryDetail)
    waitForMock.mockReset(); waitForMock.mockResolvedValue({
      id: 7, status: 2, city: '成都', days: 1, persons: 2,
      dayList: [day(1, ['西湖'])], budgetList: [], totalAmount: 0,
    } as unknown as ItineraryDetail)
    getDetailMock.mockReset(); getDetailMock.mockResolvedValue({
      id: 7, status: 2, city: '成都', days: 1, persons: 2,
      dayList: [day(1, ['西湖'])], budgetList: [], totalAmount: 0,
    } as unknown as ItineraryDetail)
    streamMock.mockReset(); streamMock.mockResolvedValue(undefined)
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  /** 走到 confirm 态并改动一次表单（微调抽屉点一个偏好 chip → updateSlots）。 */
  async function reachDirtyConfirm(container: HTMLDivElement) {
    await say(container, '国庆想去成都玩 4 天，两个人')
    expect(container.querySelector('.intake-confirm')).not.toBeNull()
    await act(async () => { (container.querySelector('.intake-tune-toggle') as HTMLButtonElement).click() })
    await act(async () => { (container.querySelector('.intake-prefs button') as HTMLButtonElement).click() })
    expect((container.querySelector('.intake-start') as HTMLButtonElement).textContent).toContain('信息已更新，重新确认')
    expect(clarifyMock).toHaveBeenCalledTimes(1)
  }

  it('② 表单修改后 ready 失效：开工先重验，重验成功（ready 恢复）才触发生成', async () => {
    clarifyMock.mockResolvedValue(clarifyReady)
    const { container } = await mountStudio()
    await reachDirtyConfirm(container)

    // 点开工：先打一轮 clarify 重验，ready 恢复后才 submit 生成
    await act(async () => { (container.querySelector('.intake-start') as HTMLButtonElement).click() })
    await flush()
    expect(clarifyMock).toHaveBeenCalledTimes(2)
    expect(clarifyMock.mock.calls[1][0]).toBe('我更新了行程信息，请重新确认')
    expect(generateMock).toHaveBeenCalledTimes(1)
  })

  it('②b 重验未过保持禁用：不触发生成，确认卡留在待重验态可再次重验', async () => {
    clarifyMock.mockResolvedValueOnce(clarifyReady).mockResolvedValueOnce(clarifyBudgetLow)
    const { container } = await mountStudio()
    await reachDirtyConfirm(container)

    await act(async () => { (container.querySelector('.intake-start') as HTMLButtonElement).click() })
    await flush()
    expect(clarifyMock).toHaveBeenCalledTimes(2)
    expect(generateMock).not.toHaveBeenCalled()
    // needsReconfirm 兜住 confirm 态：确认卡还在，开工键回到待重验话术（可再次重验）
    expect(container.querySelector('.intake-confirm')).not.toBeNull()
    expect((container.querySelector('.intake-start') as HTMLButtonElement).textContent).toContain('信息已更新，重新确认')
  })
})
