import { describe, expect, it } from 'vitest'
import { ReactApiError } from '../../api/sinan'
import { describeLiveQuotesError, liveQuotesCaption, quoteRowMeta } from './hotelLiveQuotes'
import type { LiveHotelQuotes } from '../../types/itinerary'

/** 酒店实时价的纯视图逻辑（L16）：限速措辞、口径回显、缺席键不占位。 */
describe('describeLiveQuotesError', () => {
  it('429 是分钟窗限速（产品语义内），文案不吓人', () => {
    expect(describeLiveQuotesError(new ReactApiError('查询过于频繁，请稍后再试', 429))).toBe('查得太快了，稍等一下再试。')
  })

  it('400 等业务错误透出后端 message（如行程缺日期）', () => {
    expect(describeLiveQuotesError(new ReactApiError('本行程缺少入住/离店日期，无法查询酒店实时价', 400)))
      .toBe('本行程缺少入住/离店日期，无法查询酒店实时价')
  })

  it('非 API 错误兜底成中性文案', () => {
    expect(describeLiveQuotesError(new Error('boom'))).toBe('boom')
    expect(describeLiveQuotesError('weird')).toBe('实时价暂时查不到。')
  })
})

describe('liveQuotesCaption', () => {
  it('回显城市与入/离窗（窗口来自行程自身）', () => {
    const data = { hotelQuotes: [], city: '成都', checkIn: '2026-10-01', checkOut: '2026-10-03', reason: null } as LiveHotelQuotes
    expect(liveQuotesCaption(data)).toBe('成都 · 2026-10-01 入住 / 2026-10-03 离店')
  })
})

describe('quoteRowMeta', () => {
  it('评分与来源都有则拼接，缺席键不占位', () => {
    expect(quoteRowMeta({ name: 'A', nightlyPrice: 300, rating: 4.7, source: 'google_hotels' })).toBe('4.7 分 · google_hotels')
    expect(quoteRowMeta({ name: 'B', nightlyPrice: 300, rating: null, source: null })).toBe('')
    expect(quoteRowMeta({ name: 'C', nightlyPrice: 300 })).toBe('')
  })
})
