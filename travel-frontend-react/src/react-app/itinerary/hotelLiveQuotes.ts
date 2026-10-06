/** 酒店实时价（L16）的纯视图逻辑：状态、错误文案、行元信息——零依赖可单测。 */
import { ReactApiError, isOfflineError } from '../../api/sinan'
import type { LiveHotelQuoteRow, LiveHotelQuotes } from '../../types/itinerary'

export interface LiveQuotesState {
  status: 'idle' | 'loading' | 'ready' | 'error'
  quotes?: LiveHotelQuotes
  message?: string
}

/** 错误 → 用户文案：429 是产品语义内的限速（分钟窗），不是故障，措辞不吓人。 */
export function describeLiveQuotesError(err: unknown): string {
  if (isOfflineError(err)) return '暂时连不上规划服务，稍后再试。'
  if (err instanceof ReactApiError) {
    if (err.status === 429) return '查得太快了，稍等一下再试。'
    return err.message
  }
  return err instanceof Error ? err.message : '实时价暂时查不到。'
}

/** 结果块标题：城市 + 入/离窗（窗口来自行程自身，如实回显口径）。 */
export function liveQuotesCaption(data: LiveHotelQuotes): string {
  return `${data.city} · ${data.checkIn} 入住 / ${data.checkOut} 离店`
}

/** 行副信息（评分/来源）：缺席键不占位，留空返回空串。 */
export function quoteRowMeta(row: LiveHotelQuoteRow): string {
  const parts: string[] = []
  if (row.rating != null) parts.push(`${row.rating} 分`)
  if (row.source) parts.push(String(row.source))
  return parts.join(' · ')
}
