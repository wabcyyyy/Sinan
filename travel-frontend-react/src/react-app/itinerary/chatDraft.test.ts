import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ChatDayPlan, ItineraryChatMessage } from '../../types/chat'
import type { DayPlan, HotelOption } from '../../types/itinerary'
import {
  activeActionIndex,
  draftChanges,
  structuredDraftChanges,
  describeStructuredChange,
  diffBadge,
  hotelDefaultSelection,
  pendingActionSummary,
  unverifiedNames,
} from './chatDraft'
import { ChatPanel, DraftCard } from './ChatPanel'

/**
 * CH3/L2 对话编排的纯函数与确认卡渲染测试（同 TripBadges.test.ts 纪律：
 * renderToStaticMarkup + 零新依赖，不跑 effect、不依赖网络）。
 */

const tripDay = (dayNo: number, pois: string[]): DayPlan =>
  ({ dayId: dayNo, dayNo, generationStatus: 'SUCCEEDED', items: pois.map((poiName) => ({ itemType: 'attraction', poiName })) }) as DayPlan

const draftDay = (dayNo: number, items: Array<{ name: string; lat?: number | null }>): ChatDayPlan =>
  ({ day_no: dayNo, items: items.map((item) => ({ poi_name: item.name, latitude: item.lat ?? null, longitude: item.lat ?? null })) })

/** 带时间/费用/类型的基线日（M4 五类差异用）；字段名与契约 TripItem 的 camelCase 一致。 */
const richTripDay = (dayNo: number, items: Array<Record<string, unknown>>): DayPlan =>
  ({ dayId: dayNo, dayNo, items: items.map((item) => ({ itemType: 'attraction', ...item })) }) as unknown as DayPlan

describe('activeActionIndex（当前唯一待确认 = 最近一条带草稿的 AI 消息）', () => {
  it('倒序找最近一条；用户消息与纯文本 AI 消息跳过', () => {
    const msgs: ItineraryChatMessage[] = [
      { role: 'user', content: '改行程' },
      { role: 'ai', content: '好的', plans: [draftDay(1, [{ name: 'a' }])] },
      { role: 'user', content: '再改' },
      { role: 'ai', content: '新建议' },
    ]
    expect(activeActionIndex(msgs)).toBe(1)
    expect(activeActionIndex([{ role: 'ai', content: '没有草稿' }])).toBe(-1)
  })
})

describe('draftChanges（草稿 vs 现行程）', () => {
  it('逐天报告新增与删除', () => {
    const current = [tripDay(1, ['宽窄巷子', '人民公园'])]
    const draft = [draftDay(1, [{ name: '宽窄巷子' }, { name: '博物馆' }])]
    const changes = draftChanges(current, draft)
    expect(changes).toContain('第 1 天新增：博物馆')
    expect(changes).toContain('第 1 天删除：人民公园')
  })
  it('看不出差异给兜底文案', () => {
    expect(draftChanges([tripDay(1, ['西湖'])], [draftDay(1, [{ name: '西湖' }])])).toEqual(['计划内容已更新，请核对下方完整安排'])
  })
})

describe('structuredDraftChanges（结构化草稿差异：M4 五类）', () => {
  it('added/removed：按天分组的新增与移除，语义与旧版一致', () => {
    const current = [tripDay(1, ['宽窄巷子', '人民公园'])]
    const draft = [draftDay(1, [{ name: '宽窄巷子' }, { name: '博物馆' }])]
    const structured = structuredDraftChanges(current, draft)
    expect(structured.length).toBe(2)
    expect(structured.find((s) => s.type === 'added')?.items).toEqual(['博物馆'])
    expect(structured.find((s) => s.type === 'removed')?.items).toEqual(['人民公园'])
  })

  it('updated：同名同天时间/费用变化各一条，带 from/to；两侧缺值不算变化', () => {
    const current = [richTripDay(1, [{ poiName: '博物馆', startTime: '09:00', endTime: '10:00', cost: 80 }])]
    const draft: ChatDayPlan[] = [
      { day_no: 1, items: [{ poi_name: '博物馆', start_time: '10:00', end_time: '11:00', cost: 100 }] },
    ]
    const changes = structuredDraftChanges(current, draft)
    const timeChange = changes.find((c) => c.type === 'updated' && c.field === 'time')
    expect(timeChange?.from).toBe('09:00-10:00')
    expect(timeChange?.to).toBe('10:00-11:00')
    expect(timeChange?.items).toEqual(['博物馆'])
    const costChange = changes.find((c) => c.type === 'updated' && c.field === 'cost')
    expect(costChange?.from).toBe('80')
    expect(costChange?.to).toBe('100')
    // 草稿没给时间/费用时不误报
    const bare: ChatDayPlan[] = [{ day_no: 1, items: [{ poi_name: '博物馆' }] }]
    expect(structuredDraftChanges(current, bare)).toEqual([
      { type: 'updated', label: '计划内容已更新', items: ['请核对下方完整安排'] },
    ])
  })

  it('moved：同名换天带 fromDay/toDay，且不再重复计增删', () => {
    const current = [tripDay(1, ['灵隐寺', '西湖']), tripDay(2, ['宋城'])]
    const draft = [draftDay(1, [{ name: '西湖' }, { name: '宋城' }]), draftDay(2, [{ name: '灵隐寺' }])]
    const changes = structuredDraftChanges(current, draft)
    const moved = changes.filter((c) => c.type === 'moved')
    expect(moved).toHaveLength(2)
    const lingyin = moved.find((m) => m.items[0] === '灵隐寺')
    expect(lingyin?.fromDay).toBe(1)
    expect(lingyin?.toDay).toBe(2)
    const songcheng = moved.find((m) => m.items[0] === '宋城')
    expect(songcheng?.fromDay).toBe(2)
    expect(songcheng?.toDay).toBe(1)
    expect(changes.some((c) => c.type === 'added' || c.type === 'removed')).toBe(false)
  })

  it('hotelChanged：酒店条目只比身份（fromName/toName），不进逐日增删', () => {
    const current = [richTripDay(1, [{ poiName: '如归客栈', itemType: 'hotel' }])]
    const draft: ChatDayPlan[] = [{ day_no: 1, items: [{ poi_name: '锦江宾馆', item_type: 'hotel' }] }]
    const changes = structuredDraftChanges(current, draft)
    expect(changes).toHaveLength(1)
    expect(changes[0].type).toBe('hotelChanged')
    expect(changes[0].fromName).toBe('如归客栈')
    expect(changes[0].toName).toBe('锦江宾馆')
  })

  it('describe/diffBadge：五类文案（费用带 ¥）与徽标；中性兜底条目不出徽标', () => {
    expect(describeStructuredChange({ type: 'added', label: '第 2 天新增', items: ['博物馆'] })).toBe('第 2 天新增：博物馆')
    expect(describeStructuredChange({ type: 'removed', label: '第 2 天移除', items: ['断桥'] })).toBe('第 2 天移除：断桥')
    expect(describeStructuredChange({ type: 'updated', dayNo: 1, field: 'time', label: '第 1 天', items: ['博物馆'], from: '09:00-10:00', to: '10:00-11:00' }))
      .toBe('博物馆（第 1 天）：09:00-10:00→10:00-11:00')
    expect(describeStructuredChange({ type: 'updated', dayNo: 1, field: 'cost', label: '第 1 天', items: ['博物馆'], from: '80', to: '100' }))
      .toBe('博物馆（第 1 天）：¥80→¥100')
    expect(describeStructuredChange({ type: 'moved', label: '灵隐寺', items: ['灵隐寺'], fromDay: 1, toDay: 2 }))
      .toBe('灵隐寺：第 1 天→第 2 天')
    expect(describeStructuredChange({ type: 'hotelChanged', label: '住宿调整', items: [], fromName: '如归客栈', toName: '锦江宾馆' }))
      .toBe('住宿调整：如归客栈→锦江宾馆')
    expect(diffBadge({ type: 'added', label: '', items: [] })?.text).toBe('+新增')
    expect(diffBadge({ type: 'removed', label: '', items: [] })?.text).toBe('−移除')
    expect(diffBadge({ type: 'updated', field: 'time', from: 'a', to: 'b', label: '', items: [] })?.text).toBe('~时间')
    expect(diffBadge({ type: 'updated', field: 'cost', from: '80', to: '100', label: '', items: [] })?.className).toBe('is-update')
    expect(diffBadge({ type: 'moved', label: '', items: [] })?.text).toBe('→跨日')
    expect(diffBadge({ type: 'hotelChanged', label: '', items: [] })?.text).toBe('酒店')
    expect(diffBadge({ type: 'updated', label: '计划内容已更新', items: ['请核对下方完整安排'] })).toBeNull()
  })
})

describe('unverifiedNames（AI 新增且无坐标 = 未核实）', () => {
  it('只报新增且缺坐标的点位', () => {
    const current = [tripDay(1, ['宽窄巷子'])]
    const draft = [draftDay(1, [{ name: '宽窄巷子' }, { name: '新点位', lat: 30.1 }, { name: '没坐标的新点位' }])]
    expect(unverifiedNames(current, draft)).toEqual(['没坐标的新点位（第 1 天）'])
  })
})

describe('hotelDefaultSelection（酒店候选默认选中）', () => {
  const option = {
    id: 1,
    hotelName: '锦江宾馆',
    tier: '舒适型',
    nights: 2,
    requestedDayNos: [1, 2],
    roomTypes: [
      { id: 11, roomName: '大床房', isDefault: false, totalPrice: 800 },
      { id: 12, roomName: '双床房', isDefault: true, totalPrice: 900 },
    ],
  } as unknown as HotelOption

  it('默认房型 = isDefault 优先；晚次取后端给的 requestedDayNos', () => {
    expect(hotelDefaultSelection(option, 3)).toEqual({ roomType: '双床房', dayNos: [1, 2] })
  })
  it('后端没给晚次时按天数推导，且不超行程天数', () => {
    const bare = { ...option, requestedDayNos: undefined, nights: 9 } as unknown as HotelOption
    const selection = hotelDefaultSelection(bare, 2)
    expect(selection.dayNos).toEqual([1, 2])
    expect(selection.roomType).toBe('双床房')
  })
})

describe('pendingActionSummary（确认卡一句话）', () => {
  it('拼出换酒店提案摘要', () => {
    expect(
      pendingActionSummary({ type: 'replace_hotel', hotel_names: ['亚朵', '全季'], target_tier: '高档型', night_count: 2 }),
    ).toBe('AI 提议更换住宿：亚朵、全季（高档型），共 2 晚')
  })
})

describe('ChatPanel/L2：确认卡渲染（draft 带 requiresConfirmation → 确认卡长在对话流内）', () => {
  const dayList = [tripDay(1, ['西湖'])]
  const confirmMsg: ItineraryChatMessage = {
    id: 2,
    role: 'ai',
    content: '给你一个高档型候选，点选即确认。',
    hotelOptions: [{
      id: 9,
      hotelName: '西湖国宾馆',
      tier: '高档型',
      nights: 1,
      totalPrice: 1600,
      requestedDayNos: [1],
      roomTypes: [{ id: 91, roomName: '湖景大床房', isDefault: true, totalPrice: 1600 }],
      baseRevision: 'rev-1',
    }] as unknown as HotelOption[],
    baseRevision: 'rev-1',
    requiresConfirmation: true,
    pendingAction: { type: 'replace_hotel', hotel_names: ['西湖国宾馆'], target_tier: '高档型', day_numbers: [1], night_count: 1, requires_confirmation: true },
  }

  it('确认横幅（提案摘要）与酒店选择器渲染，房型默认选中，无计划应用按钮', () => {
    const html = renderToStaticMarkup(
      createElement(DraftCard, {
        msg: confirmMsg,
        itineraryId: 7,
        dayList,
        applying: false,
        onApply: () => undefined,
        onApplyHotel: () => undefined,
      }),
    )
    expect(html).toContain('需要你确认')
    expect(html).toContain('AI 提议更换住宿：西湖国宾馆（高档型），共 1 晚')
    expect(html).toContain('西湖国宾馆')
    expect(html).toContain('湖景大床房')
    expect(html).toContain('确认入住')
    expect(html).toContain('查实时价')
    expect(html).not.toContain('应用到行程')
  })

  it('L16：候选带 searchLink 出「地图核实」深链，缺 link 如实不出', () => {
    const withLink = renderToStaticMarkup(
      createElement(DraftCard, {
        msg: { ...confirmMsg, hotelOptions: [{ ...confirmMsg.hotelOptions![0], searchLink: 'https://amap.com/search?query=%E8%A5%BF%E6%B9%96%E5%9B%BD%E5%AE%BE%E9%A6%86' }] } as ItineraryChatMessage,
        itineraryId: 7,
        dayList,
        applying: false,
        onApply: () => undefined,
        onApplyHotel: () => undefined,
      }),
    )
    expect(withLink).toContain('地图核实')
    expect(withLink).toContain('https://amap.com/search')
    const withoutLink = renderToStaticMarkup(
      createElement(DraftCard, { msg: confirmMsg, itineraryId: 7, dayList, applying: false, onApply: () => undefined, onApplyHotel: () => undefined }),
    )
    expect(withoutLink).not.toContain('地图核实')
  })

  it('纯计划草稿渲染 diff 与应用按钮，不出确认横幅', () => {
    const planMsg: ItineraryChatMessage = {
      id: 3,
      role: 'ai',
      content: '建议如下',
      plans: [draftDay(1, [{ name: '博物馆' }])],
      changed: true,
      baseRevision: 'rev-2',
    }
    const html = renderToStaticMarkup(
      createElement(DraftCard, { msg: planMsg, itineraryId: 7, dayList, applying: false, onApply: () => undefined, onApplyHotel: () => undefined }),
    )
    expect(html).toContain('第 1 天新增：博物馆')
    expect(html).toContain('应用到行程')
    expect(html).not.toContain('需要你确认')
  })

  it('M4：差异卡按五类渲染徽标（+新增/−移除/~时间|费用 from→to/→跨日/酒店），不出「正式落库」措辞', () => {
    const m4DayList = [
      richTripDay(1, [
        { poiName: '灵隐寺', startTime: '09:00', endTime: '10:00', cost: 80 },
        { poiName: '苏堤' },
        { poiName: '如归客栈', itemType: 'hotel' },
      ]),
      richTripDay(2, [{ poiName: '宋城' }, { poiName: '断桥' }]),
    ]
    const planMsg: ItineraryChatMessage = {
      id: 5,
      role: 'ai',
      content: '调整如下',
      plans: [
        {
          day_no: 1,
          items: [
            { poi_name: '灵隐寺', start_time: '10:00', end_time: '11:00', cost: 100 },
            { poi_name: '锦江宾馆', item_type: 'hotel' },
          ],
        },
        { day_no: 2, items: [{ poi_name: '雷峰塔' }, { poi_name: '宋城' }, { poi_name: '苏堤' }] },
      ],
      changed: true,
      baseRevision: 'rev-3',
    }
    const html = renderToStaticMarkup(
      createElement(DraftCard, { msg: planMsg, itineraryId: 7, dayList: m4DayList, applying: false, onApply: () => undefined, onApplyHotel: () => undefined }),
    )
    expect(html).toContain('diff-badge is-add">+新增</span>')
    expect(html).toContain('第 2 天新增：雷峰塔')
    expect(html).toContain('diff-badge is-remove">−移除</span>')
    expect(html).toContain('第 2 天移除：断桥')
    expect(html).toContain('diff-badge is-update">~时间</span>')
    expect(html).toContain('灵隐寺（第 1 天）：09:00-10:00→10:00-11:00')
    expect(html).toContain('~费用</span>')
    expect(html).toContain('¥80→¥100')
    expect(html).toContain('diff-badge is-move">→跨日</span>')
    expect(html).toContain('苏堤：第 1 天→第 2 天')
    expect(html).toContain('diff-badge is-hotel">酒店</span>')
    expect(html).toContain('住宿调整：如归客栈→锦江宾馆')
    expect(html).toContain('应用到行程')
    expect(html).not.toContain('正式落库')
  })

  it('ChatPanel 静态冒烟：初始空态与输入框存在', () => {
    const html = renderToStaticMarkup(
      createElement(ChatPanel, { itineraryId: 7, dayList, onApplied: () => undefined, onReconcile: () => undefined }),
    )
    expect(html).toContain('对话编排')
    expect(html).toContain('对行程说话')
  })
})
