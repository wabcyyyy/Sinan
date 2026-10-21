import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ItineraryDetail, TripItem } from '../../types/itinerary'
import { budgetCaption, DietaryEvidenceNotice, DraftOnlyBanner, DRAFT_ONLY_TEXT, ItemEvidence, itemEvidenceText, TripMetrics } from './TripBadges'

/**
 * React 树的第一批测试（审查 P1-10：React 零测试）。
 *
 * 用 react-dom/server 静态渲染断言文案——不引入 testing-library 等新依赖，
 * 也不依赖网络/路由；被测片段是纯展示组件（TripBadges）。
 */

const item = (fields: Partial<TripItem> = {}): TripItem =>
  ({ itemType: 'attraction', poiName: '西湖', ...fields }) as TripItem

describe('React 详情页文案与徽章', () => {
  it('只有交通估算时提示缺价，不把用户预算冒充估算总额', () => {
    const trip = { budget: 1800, totalAmount: 180, budgetList: [], dayList: [{ items: [item({ cost: null })] }] } as unknown as ItineraryDetail
    const render = (data: ItineraryDetail) => renderToStaticMarkup(createElement(TripMetrics, { trip: data, dateText: null, weatherDays: 2, hasWeather: false }))
    expect(render(trip)).toContain('部分费用估算 · 尚有条目未计价')
    expect(render({ ...trip, totalAmount: 0 })).toContain('待估算')
    const free = render({ ...trip, totalAmount: 0, dayList: [{ items: [item({ cost: 0 })] }] } as ItineraryDetail)
    expect(free).toContain('￥0')
    expect(free).not.toContain('￥1800')
  })
  it('素食未验证时具体列出餐饮候选，不被一般核实横幅掩盖', () => {
    const message = '第 1 天：「餐厅」尚无素食可用性依据，不能确认满足饮食要求'
    const trip = { qualityReport: { warnings: [{ code: 'DIETARY_EVIDENCE_REVIEW', message }] } } as unknown as ItineraryDetail
    const html = renderToStaticMarkup(createElement(DietaryEvidenceNotice, { trip }))
    expect(html).toContain(message)
    expect(html).toContain('aria-label="饮食要求核实"')
    expect(renderToStaticMarkup(createElement(DietaryEvidenceNotice, { trip: {} as ItineraryDetail }))).toBe('')
    const blocked = { qualityStatus: 'BLOCKED', qualityReport: { blockingIssues: [
      { code: 'DIETARY_EVIDENCE_REVIEW', message },
    ] } } as unknown as ItineraryDetail
    expect(renderToStaticMarkup(createElement(DietaryEvidenceNotice, { trip: blocked })))
      .toContain('行程未完成，请核实餐饮与饮食要求')
  })
  it('预算告警消费后端的金额与口径，生成中也可见', () => {
    const message = '当前估算总额 ￥640.00 超出预算 ￥500.00，超出 ￥140.00；请调整行程或预算'
    const trip = { totalAmount: 640, budget: 500, qualityReport: {
      qualityStatus: 'DRAFT', warnings: [{ code: 'BUDGET_EXCEEDED', message }],
    } } as unknown as ItineraryDetail
    const html = renderToStaticMarkup(createElement(TripMetrics, { trip, dateText: '10月1日', weatherDays: 2, hasWeather: false }))
    expect(html).toContain(message)
    expect(html).toContain('role="status"')
    expect(html).toContain('fact-warning')
  })
  it('预算行带估算字样（P1-4：此前只有「预算参考」）', () => {
    const trip = { totalAmount: 3200 } as unknown as ItineraryDetail
    const html = renderToStaticMarkup(
      createElement(TripMetrics, { trip, dateText: '10月1日', weatherDays: 5, hasWeather: false }),
    )
    expect(html).toContain('￥3200')
    expect(html).toContain(budgetCaption())
    expect(budgetCaption()).toContain('估算')
    expect(html).toContain('路线草案')
  })

  it('destinationStatus=draft_only 渲染未核实横幅，其他状态不渲染（P1-2）', () => {
    const shown = renderToStaticMarkup(createElement(DraftOnlyBanner, { status: 'draft_only' }))
    expect(shown).toContain(DRAFT_ONLY_TEXT)
    expect(shown).toContain('未核实草案')
    expect(renderToStaticMarkup(createElement(DraftOnlyBanner, { status: 'researched' }))).toBe('')
    expect(renderToStaticMarkup(createElement(DraftOnlyBanner, { status: null }))).toBe('')
  })

  it('项级徽章走共享映射，估算项带警示类', () => {
    expect(itemEvidenceText(item({ valueKind: 'estimated' }))).toBe('估算信息')
    expect(itemEvidenceText(item({ verificationStatus: 'unverified' }))).toBe('待核实')
    expect(itemEvidenceText(item({ valueKind: 'observed', verificationStatus: 'partially_verified' }))).toBe(
      '部分信息有据',
    )
    // 无任何标签信号时退回「已核实」文案，但不冒充有来源
    expect(itemEvidenceText(item())).toBe('已核实')
    const html = renderToStaticMarkup(createElement(ItemEvidence, { item: item({ valueKind: 'estimated' }) }))
    expect(html).toContain('fact-warning')
    expect(html).toContain('估算信息')
  })
})

describe('详情页核实提示合并与空日期（视觉体检 2026-10-11）', () => {
  it('草案提示带上待核实条数，0 条时不出数字', () => {
    const html = renderToStaticMarkup(createElement(DraftOnlyBanner, { status: 'draft_only', pending: 3 }))
    expect(html).toContain(DRAFT_ONLY_TEXT)
    expect(html).toContain('3 项待核实')
    expect(renderToStaticMarkup(createElement(DraftOnlyBanner, { status: 'draft_only' }))).not.toContain('项待核实')
  })
  it('没有出发日期时不单占「待定」卡，并进天数卡说明', () => {
    const trip = { totalAmount: 3200 } as unknown as ItineraryDetail
    const html = renderToStaticMarkup(createElement(TripMetrics, { trip, dateText: null, weatherDays: 4, hasWeather: false }))
    expect(html).not.toContain('出发日期')
    expect(html).not.toContain('待定')
    expect(html).toContain('日期未定 · 路线草案')
    expect(html).toContain(budgetCaption())
  })
})
