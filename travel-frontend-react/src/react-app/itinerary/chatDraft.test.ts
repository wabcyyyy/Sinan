import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ChatDayPlan, ItineraryChatMessage } from '../../types/chat'
import type { DayPlan, HotelOption } from '../../types/itinerary'
import {
  activeActionIndex,
  draftChanges,
  structuredDraftChanges,
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

describe('structuredDraftChanges（结构化草稿差异）', () => {
  it('产出结构化对象供现代卡片渲染状态标签', () => {
    const current = [tripDay(1, ['宽窄巷子', '人民公园'])]
    const draft = [draftDay(1, [{ name: '宽窄巷子' }, { name: '博物馆' }])]
    const structured = structuredDraftChanges(current, draft)
    expect(structured.length).toBe(2)
    expect(structured.find((s) => s.type === 'add')?.items).toEqual(['博物馆'])
    expect(structured.find((s) => s.type === 'remove')?.items).toEqual(['人民公园'])
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

  it('ChatPanel 静态冒烟：初始空态与输入框存在', () => {
    const html = renderToStaticMarkup(
      createElement(ChatPanel, { itineraryId: 7, dayList, onApplied: () => undefined, onReconcile: () => undefined }),
    )
    expect(html).toContain('对话编排')
    expect(html).toContain('对行程说话')
  })
})
