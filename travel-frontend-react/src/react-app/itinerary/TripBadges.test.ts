import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ItineraryDetail, TripItem } from '../../types/itinerary'
import { budgetCaption, DraftOnlyBanner, DRAFT_ONLY_TEXT, ItemEvidence, itemEvidenceText, TripMetrics } from './TripBadges'

/**
 * React 树的第一批测试（审查 P1-10：React 零测试）。
 *
 * 用 react-dom/server 静态渲染断言文案——不引入 testing-library 等新依赖，
 * 也不依赖网络/路由；被测片段是纯展示组件（TripBadges）。
 */

const item = (fields: Partial<TripItem> = {}): TripItem =>
  ({ itemType: 'attraction', poiName: '西湖', ...fields }) as TripItem

describe('React 详情页文案与徽章', () => {
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