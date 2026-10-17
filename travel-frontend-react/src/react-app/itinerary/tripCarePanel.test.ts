import { act } from 'react'
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getPreferenceMemory, getTripCare } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { DayPlan } from '../../types/itinerary'
import { IntakeConfirm, addMemoryLabel } from '../home/IntakeConfirm'
import { isCareDraft } from './chatDraft'
import { DraftCard } from './ChatPanel'
import {
  CARE_DRAFT_COPY,
  CarePanelView,
  TripCarePanel,
  careAffectedNames,
  careAttractions,
  careDraftState,
  carePanelVisible,
  careResultLine,
  careRiskRows,
  formatCareDay,
  formatCareTime,
} from './TripCarePanel'

/**
 * 出发前照看面板（个人 Agent V1）的前端纪律测试（同 chatDraft.test.ts 口径）：
 * 纯函数 + renderToStaticMarkup 静态渲染；交互面用 createRoot + mock api 层
 * （同 useTripChat.test.ts 口径）验证「加载取数 / 仅 checking 短轮询 / 静态展示隔离」。
 * 后端语义真源是 BE app/services/itinerary_care*.py；规格 §5 的固定文案在这里锁定。
 */

vi.mock('../../api/sinan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sinan')>()
  return {
    ...actual,
    getTripCare: vi.fn(),
    requestTripCareCheck: vi.fn(),
    updateTripCare: vi.fn(),
    dismissTripCare: vi.fn(),
    getPreferenceMemory: vi.fn(),
    savePreferenceMemory: vi.fn(),
  }
})

const getCareMock = vi.mocked(getTripCare)

// 同 useTripChat.test.ts：createRoot + act 需显式声明 act 环境
;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true

const day = (dayNo: number, items: Array<{ id?: number; poiName: string; itemType?: string }>): DayPlan =>
  ({ dayId: dayNo, dayNo, items: items.map((item) => ({ itemType: 'attraction', ...item })) }) as DayPlan

const tripDays = [
  day(1, [{ id: 11, poiName: '宽窄巷子' }, { id: 12, poiName: '人民公园' }]),
  day(2, [{ id: 21, poiName: '大熊猫基地' }, { poiName: '无主安排' }]),
]

const careState = (overrides: Partial<Contracts.CareStateVO> = {}): Contracts.CareStateVO => ({
  supported: true,
  enabled: false,
  watchedItemIds: [],
  status: null,
  nextCheckAt: null,
  lastCheckedAt: null,
  lastResult: null,
  needsReselect: false,
  ...overrides,
})

const riskResult = (overrides: Partial<Contracts.CareCheckResult> = {}): Contracts.CareCheckResult => ({
  checkedAt: '2026-10-17T09:30',
  status: 'no_risk',
  reason: '近期没有影响出行的降水风险',
  days: [
    { date: '2026-10-18', precipProb: 72.4, weatherCode: 61, condition: '小雨', risk: true },
    { date: '2026-10-19', precipProb: 10, weatherCode: 0, condition: '晴', risk: false },
  ],
  affectedItemIds: [],
  pendingMessageId: null,
  ...overrides,
})

const viewProps = (care: Contracts.CareStateVO, overrides: Record<string, unknown> = {}) => ({
  care,
  dayList: tripDays,
  selection: [...care.watchedItemIds],
  dirty: false,
  agree: false,
  busy: '' as const,
  notice: '',
  onToggleAgree: () => {},
  onToggleItem: () => {},
  onEnable: () => {},
  onDisable: () => {},
  onSaveSelection: () => {},
  onCheck: () => {},
  onDismiss: () => {},
  onViewDraft: () => {},
  onNoticeClose: () => {},
  ...overrides,
})

beforeEach(() => {
  getCareMock.mockReset()
  getCareMock.mockResolvedValue(careState({ supported: false }))
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('静态展示模式隔离（规格 §5：演示版不请求 care API、不伪装实时照看）', () => {
  it('carePanelVisible：离线样例与演示模式都不渲染面板', () => {
    expect(carePanelVisible({ offline: false, demo: false })).toBe(true)
    expect(carePanelVisible({ offline: true, demo: false })).toBe(false)
    expect(carePanelVisible({ offline: false, demo: true })).toBe(false)
    expect(carePanelVisible({ offline: true, demo: true })).toBe(false)
  })
})

describe('固定文案（规格 §5 三条硬性口径）', () => {
  it('没有数据/已有用户草稿/额度不足按规格文案呈现', () => {
    expect(careResultLine(riskResult({ status: 'unavailable', reason: '随便什么' }))).toBe('暂未取得天气信息')
    expect(careResultLine(riskResult({ status: 'waiting_for_user', reason: '先处理当前草稿，之后会自动重试' }))).toBe('先处理当前草稿')
    expect(careResultLine(riskResult({ status: 'quota_exhausted' }))).toBe('未生成调整建议：额度不足')
  })
  it('其余状态用后端 reason（单一真源），缺省落状态兜底', () => {
    expect(careResultLine(riskResult({ status: 'waiting', reason: '将在出发前三天开始检查' }))).toBe('将在出发前三天开始检查')
    expect(careResultLine(riskResult({ status: 'failed', reason: null }))).toBe('最近一次检查未完成，将等待下一窗口')
  })
})

describe('草稿现状派生（规格 §4.3：过期结果不能显示已成功调整）', () => {
  it('派生态取自 lastResult.status 的 pending/applied/stale', () => {
    expect(careDraftState(riskResult({ status: 'pending', pendingMessageId: 9 }))).toBe('pending')
    expect(careDraftState(riskResult({ status: 'applied' }))).toBe('applied')
    expect(careDraftState(riskResult({ status: 'stale' }))).toBe('stale')
    expect(careDraftState(riskResult({ status: 'no_risk' }))).toBeNull()
    expect(careDraftState(null)).toBeNull()
  })
  it('stale 的文案是「失效且未应用」，绝不出现「已成功调整」；applied 对确认/忽略两结局都成立', () => {
    expect(CARE_DRAFT_COPY.stale).toContain('失效')
    expect(CARE_DRAFT_COPY.stale).not.toContain('成功')
    expect(CARE_DRAFT_COPY.stale).not.toContain('已应用')
    // 派生 applied 无法区分「确认应用」与「已忽略」（consume_pending_action 同现状），
    // 文案不得冒认用户没做过的「已应用」
    expect(CARE_DRAFT_COPY.applied).toContain('已处理')
    expect(CARE_DRAFT_COPY.applied).not.toContain('已确认应用')
  })
  it('静态渲染：stale 结果卡不含成功口径；pending 结果卡有查看/忽略', () => {
    const staleHtml = renderToStaticMarkup(createElement(CarePanelView, viewProps(
      careState({ enabled: true, status: 'stale', lastResult: riskResult({ status: 'stale' }) }),
    )))
    expect(staleHtml).toContain('该建议已失效，行程未按它调整')
    expect(staleHtml).not.toContain('查看调整草稿')
    expect(staleHtml).not.toContain('已成功调整')

    const pendingHtml = renderToStaticMarkup(createElement(CarePanelView, viewProps(
      careState({ enabled: true, status: 'pending', lastResult: riskResult({ status: 'pending', pendingMessageId: 9, affectedItemIds: [21] }) }),
    )))
    expect(pendingHtml).toContain('有一份天气调整建议待你确认')
    expect(pendingHtml).toContain('查看调整草稿')
    expect(pendingHtml).toContain('忽略')
    expect(pendingHtml).toContain('大熊猫基地')
  })
})

describe('受影响活动与预报依据', () => {
  it('careAffectedNames：死 ID（行程编辑后删除）不渲染，不按名称猜绑定', () => {
    expect(careAffectedNames(tripDays, [11, 21, 999])).toEqual(['宽窄巷子', '大熊猫基地'])
    expect(careAffectedNames(tripDays, [21, 21, 11])).toEqual(['大熊猫基地', '宽窄巷子'])
  })
  it('careAttractions：只收有主键的 attraction，按天次排序', () => {
    expect(careAttractions(tripDays).map((item) => item.id)).toEqual([11, 12, 21])
  })
  it('careRiskRows：只报风险日，概率取整，条件缺省不编造', () => {
    const rows = careRiskRows(riskResult({ days: [
      { date: '2026-10-18', precipProb: 72.4, weatherCode: 61, condition: '小雨', risk: true },
      { date: '2026-10-19', precipProb: 10, weatherCode: 0, condition: '晴', risk: false },
      { date: '2026-10-20', precipProb: 80, weatherCode: null, condition: null, risk: true },
    ] }))
    expect(rows).toEqual([
      { date: '2026-10-18', condition: '小雨', precip: '72%' },
      { date: '2026-10-20', condition: '预报有降水风险', precip: '80%' },
    ])
  })
  it('时间与日期格式：上海墙钟原样展示，不做时区换算', () => {
    expect(formatCareTime('2026-10-17T09:30')).toBe('2026-10-17 09:30')
    expect(formatCareTime('2026-10-17T09:30:00')).toBe('2026-10-17 09:30')
    expect(formatCareTime(null)).toBeNull()
    expect(formatCareDay('2026-10-08')).toBe('10月8日')
    expect(formatCareDay('oops')).toBe('oops')
  })
})

describe('CarePanelView 静态渲染', () => {
  it('关闭态：费用说明与额度授权勾选可见，开关可开', () => {
    const html = renderToStaticMarkup(createElement(CarePanelView, viewProps(careState())))
    expect(html).toContain('出发前照看')
    expect(html).toContain('消耗少量 AI 额度')
    expect(html).toContain('我同意后台 AI 建议可能消耗额度')
    expect(html).toContain('关注这些活动的天气影响')
  })
  it('风险观测与检查时间照实呈现；unavailable 不展示上一轮残留风险行', () => {
    const html = renderToStaticMarkup(createElement(CarePanelView, viewProps(
      careState({ enabled: true, status: 'no_risk', lastCheckedAt: '2026-10-17T09:30', lastResult: riskResult() }),
    )))
    expect(html).toContain('10月18日')
    expect(html).toContain('小雨 · 降水概率 72%')
    expect(html).toContain('2026-10-17 09:30 检查')
    expect(html).toContain('可能来自缓存')

    const unavailableHtml = renderToStaticMarkup(createElement(CarePanelView, viewProps(
      careState({ enabled: true, status: 'unavailable', lastResult: riskResult({ status: 'unavailable', reason: '暂未取得天气信息' }) }),
    )))
    expect(unavailableHtml).toContain('暂未取得天气信息')
    expect(unavailableHtml).not.toContain('降水概率')
  })
  it('needsReselect 出警示；等待窗口出固定提示', () => {
    const reselect = renderToStaticMarkup(createElement(CarePanelView, viewProps(
      careState({ enabled: true, status: 'needs_reselect', needsReselect: true }),
    )))
    expect(reselect).toContain('请重新选择后保存')
    const waiting = renderToStaticMarkup(createElement(CarePanelView, viewProps(
      careState({ enabled: true, status: 'waiting', lastResult: riskResult({ status: 'waiting', checkedAt: null, days: [], reason: '将在出发前三天开始检查' }) }),
    )))
    expect(waiting).toContain('将在出发前三天开始检查')
  })
})

describe('TripCarePanel 有状态壳：加载取数、仅 checking 短轮询、外部事件重取', () => {
  const roots: Array<{ root: Root; container: HTMLDivElement }> = []

  function mountPanel(props: { refreshKey?: number; dayList?: DayPlan[] } = {}) {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    const render = (next: { refreshKey?: number; dayList?: DayPlan[] } = {}) => {
      act(() => {
        root.render(createElement(TripCarePanel, {
          itineraryId: 7,
          dayList: next.dayList ?? tripDays,
          onViewDraft: () => {},
          refreshKey: next.refreshKey ?? 0,
        }))
      })
    }
    render(props)
    roots.push({ root, container })
    return { container, render, unmount: () => act(() => { root.unmount() }) }
  }

  afterEach(() => {
    for (const { root } of roots.splice(0).reverse()) act(() => { root.unmount() })
  })

  it('页面加载即取状态；supported=false 整块隐藏（能力不暴露）', async () => {
    getCareMock.mockResolvedValue(careState({ supported: false }))
    const { container } = mountPanel()
    await act(async () => {})
    expect(getCareMock).toHaveBeenCalledTimes(1)
    expect(getCareMock.mock.calls[0]).toEqual([7])
    expect(container.querySelector('.care-panel')).toBeNull()
  })

  it('supported=false 且照看已结束（已出发/归档）：不出操作面板，但给一句终态交代', async () => {
    getCareMock.mockResolvedValue(careState({ supported: false, status: 'finished', enabled: true }))
    const { container } = mountPanel()
    await act(async () => {})
    const panel = container.querySelector('.care-panel-ended')
    expect(panel).not.toBeNull()
    expect(container.textContent).toContain('行程已出发或已归档，照看结束')
    expect(container.textContent).not.toContain('立即检查')
    expect(container.textContent).not.toContain('我同意后台 AI 建议可能消耗额度')
  })

  it('正常读回：开关态、监控范围与结果卡可刷新后重现（刷新/重新打开读回）', async () => {
    getCareMock.mockResolvedValue(careState({
      enabled: true,
      status: 'pending',
      watchedItemIds: [11, 21],
      lastCheckedAt: '2026-10-17T08:00',
      lastResult: riskResult({ status: 'pending', pendingMessageId: 9, affectedItemIds: [11, 999] }),
    }))
    const { container } = mountPanel()
    await act(async () => {})
    const panel = container.querySelector('.care-panel')
    expect(panel).not.toBeNull()
    expect(container.textContent).toContain('已开启')
    expect(container.textContent).toContain('宽窄巷子')
    expect(container.textContent).toContain('查看调整草稿')
    // 死 ID 999 不出现，也不按名称猜绑定
    expect(container.textContent).not.toContain('无主安排')
  })

  it('仅 status=checking 时短轮询，状态离开 checking 即停', async () => {
    vi.useFakeTimers()
    try {
      getCareMock.mockResolvedValueOnce(careState({ enabled: true, status: 'checking' }))
      getCareMock.mockResolvedValue(careState({ enabled: true, status: 'no_risk', lastResult: riskResult() }))
      const { container } = mountPanel()
      await act(async () => {})
      expect(getCareMock).toHaveBeenCalledTimes(1)
      expect(container.textContent).toContain('正在检查天气…')
      // 第一跳轮询拿到 no_risk：状态离开 checking，轮询就此停住
      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(getCareMock).toHaveBeenCalledTimes(2)
      const callsAfterDone = getCareMock.mock.calls.length
      await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
      expect(getCareMock.mock.calls.length).toBe(callsAfterDone)
      expect(container.textContent).toContain('近期没有影响出行的降水风险')
      expect(container.textContent).not.toContain('正在检查天气…')
    } finally {
      vi.useRealTimers()
    }
  })

  it('回到前台时重新获取状态', async () => {
    vi.useFakeTimers()
    try {
      getCareMock.mockResolvedValue(careState({ enabled: true, status: 'no_risk' }))
      const { container } = mountPanel()
      await act(async () => {})
      expect(getCareMock).toHaveBeenCalledTimes(1)
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'))
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(getCareMock).toHaveBeenCalledTimes(2)
      expect(container.querySelector('.care-panel')).not.toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('对话里确认草稿后（refreshKey bump）面板重取状态', async () => {
    getCareMock.mockResolvedValue(careState({ enabled: true, status: 'pending', lastResult: riskResult({ status: 'pending', pendingMessageId: 9 }) }))
    const panel = mountPanel()
    await act(async () => {})
    expect(getCareMock).toHaveBeenCalledTimes(1)
    panel.render({ refreshKey: 1 })
    await act(async () => {})
    expect(getCareMock).toHaveBeenCalledTimes(2)
  })
})

describe('照看草稿长在既有对话流（不造第二套「最新草稿」语义）', () => {
  // `_confirmationThread` 是服务端随草稿持久化的内部元数据（下划线键，不入线级契约，
  // 运行时随 plans JSON 透传给前端）；夹具按 isCareDraft 的读取方式窄化构造
  const carePlans = [
    {
      day_no: 1,
      _confirmationThread: 'care-7',
      items: [{ poi_name: '宽窄巷子', item_type: 'attraction' }, { poi_name: '四川省博物院', item_type: 'attraction' }],
    },
  ] as unknown as import('../../types/chat').ChatDayPlan[]
  const careMsg = {
    id: 9,
    role: 'ai' as const,
    content: '[行程照看] 18 日有小雨，建议把户外安排挪进室内展馆。',
    plans: carePlans,
    baseRevision: 'rev-7',
  }
  const plansWithThread = (thread: string): import('../../types/chat').ChatDayPlan[] =>
    [{ _confirmationThread: thread }] as unknown as import('../../types/chat').ChatDayPlan[]
  it('isCareDraft：按服务端线程元数据识别照看建议，不按内容前缀猜', () => {
    expect(isCareDraft(careMsg)).toBe(true)
    // 用户消息带同款前缀也不算（元数据判定，前缀只是展示文案）
    expect(isCareDraft({ id: 1, role: 'user', content: '[行程照看] 用户原话', plans: plansWithThread('care-7') })).toBe(false)
    // 普通 AI 草稿：无线程元数据或 chat 线程都不算；伪装前缀的普通回复不算
    expect(isCareDraft({ id: 2, role: 'ai', content: '把第二天安排松一点' })).toBe(false)
    expect(isCareDraft({ id: 3, role: 'ai', content: '[行程照看] 伪装前缀的普通回复', plans: [{ day_no: 1, items: [] }] as import('../../types/chat').ChatDayPlan[] })).toBe(false)
    expect(isCareDraft({ id: 4, role: 'ai', content: '普通草稿', plans: plansWithThread('chat-7') })).toBe(false)
  })
  it('DraftCard 渲染照看草稿：来源标注 + 既有差异卡与「应用到行程」按钮', () => {
    const html = renderToStaticMarkup(createElement(DraftCard, {
      msg: careMsg,
      itineraryId: 7,
      dayList: tripDays,
      applying: false,
      onApply: () => {},
      onApplyHotel: () => {},
    }))
    expect(html).toContain('来自出发前照看')
    expect(html).toContain('应用到行程')
    expect(html).toContain('新增')
    expect(html).toContain('移除')
  })
})

describe('长期偏好编辑（IntakeConfirm 记忆区，规格 §3.1.2）', () => {
  it('addMemoryLabel：trim、去重、超长拒绝、正负合计上限', () => {
    expect(addMemoryLabel(['徒步'], '  徒步  ')).toEqual([['徒步'], '这个标签已经在列表里'])
    expect(addMemoryLabel([], '   ')).toEqual([[], '请先输入偏好内容'])
    expect(addMemoryLabel([], 'x'.repeat(33))).toEqual([[], '偏好标签最长 32 个字符'])
    const capped = Array.from({ length: 15 }, (_, i) => `p${i}`)
    expect(addMemoryLabel(capped, '新标签', 5)).toEqual([capped, '正负合计最多 20 项'])
    const [list, error] = addMemoryLabel(['徒步'], ' 美食 ')
    expect(error).toBe('')
    expect(list).toEqual(['徒步', '美食'])
  })
  it('确认页的长期偏好入口在非演示模式可见', () => {
    vi.stubEnv('VITE_DEMO_MODE', 'false')
    try {
      const html = renderToStaticMarkup(createElement(IntakeConfirm, {
        slots: { city: '成都', days: 2, persons: 2, preferences: [] },
        busy: false,
        ready: true,
        needsReconfirm: false,
        onSlots: () => {},
        onReset: () => {},
        onReconfirm: async () => true,
        onStart: () => {},
      }))
      expect(html).toContain('有想长期记住的旅行偏好')
    } finally {
      vi.unstubAllEnvs()
    }
  })
  it('演示模式不渲染长期偏好入口（不调用记忆 API）', () => {
    vi.stubEnv('VITE_DEMO_MODE', 'true')
    try {
      const html = renderToStaticMarkup(createElement(IntakeConfirm, {
        slots: { city: '成都', days: 2, persons: 2, preferences: [] },
        busy: false,
        ready: true,
        needsReconfirm: false,
        onSlots: () => {},
        onReset: () => {},
        onReconfirm: async () => true,
        onStart: () => {},
      }))
      expect(html).not.toContain('有想长期记住的旅行偏好')
      expect(html).not.toContain('intake-memory')
    } finally {
      vi.unstubAllEnvs()
    }
  })
})

describe('长期偏好在确认页复用（规格 §3.1.5：新行程展示为可选建议、选入才生效）', () => {
  const memoryMock = vi.mocked(getPreferenceMemory)

  function mountConfirm(onSlots: (patch: Record<string, unknown>) => void) {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    act(() => {
      root.render(createElement(IntakeConfirm, {
        slots: { city: '成都', days: 2, persons: 2, preferences: [] },
        busy: false,
        ready: true,
        needsReconfirm: false,
        onSlots,
        onReset: () => {},
        onReconfirm: async () => true,
        onStart: () => {},
      }))
    })
    return { container, unmount: () => act(() => { root.unmount() }) }
  }

  it('展开时读取记忆并渲染；只有点「选入本次」才进入本次偏好（本次输入优先），避开区没有选入按钮', async () => {
    vi.stubEnv('VITE_DEMO_MODE', 'false')
    memoryMock.mockResolvedValue({ positive: ['美食'], negative: ['购物'] })
    const onSlots = vi.fn()
    const mounted = mountConfirm(onSlots)
    try {
      // 页面上有两个 .intake-tune-toggle（参数微调抽屉 + 长期偏好抽屉），必须点中后者
      const toggle = mounted.container.querySelector<HTMLButtonElement>('.intake-memory-drawer .intake-tune-toggle')
      expect(toggle).not.toBeNull()
      expect(memoryMock).not.toHaveBeenCalled()
      await act(async () => { toggle!.click() })
      expect(memoryMock).toHaveBeenCalledTimes(1)
      expect(mounted.container.textContent).toContain('美食')
      expect(mounted.container.textContent).toContain('购物')
      // 未点选入：长期记忆不自动注入本次行程
      expect(onSlots).not.toHaveBeenCalled()
      const adopt = mounted.container.querySelector<HTMLButtonElement>(
        '.intake-memory-chip.is-positive .intake-memory-adopt',
      )
      expect(adopt).not.toBeNull()
      expect(mounted.container.querySelectorAll('.intake-memory-chip.is-negative .intake-memory-adopt')).toHaveLength(0)
      await act(async () => { adopt!.click() })
      expect(onSlots).toHaveBeenCalledWith({ preferences: ['美食'] })
    } finally {
      mounted.unmount()
      vi.unstubAllEnvs()
    }
  })
})
