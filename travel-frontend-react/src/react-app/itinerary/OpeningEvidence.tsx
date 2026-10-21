import type { TripItem } from '../../types/itinerary'

export function OpeningEvidence({ item }: { item: TripItem }) {
  const fact = item.factEvidence?.open_time
  if (!fact?.openingHours) return null
  let source: string | null = null
  try {
    const url = new URL(fact.sourceUrl || '')
    if (['https:', 'http:'].includes(url.protocol)) source = url.href
  } catch { /* 缺少可打开的来源时只显示待核实说明。 */ }
  const excerpt = fact.excerpt ? fact.excerpt.slice(0, 400) + (fact.excerpt.length > 400 ? '…' : '') : null
  const known = fact.openingHours.status === 'known'
  return <aside className="day-note is-tips opening-evidence" aria-label="开放资料" onClick={(event) => event.stopPropagation()}><div>
    <strong>{known ? '开放资料 · 网页规则' : '开放资料待确认'}</strong>
    <p>{(known ? excerpt : fact.openingHours.reason) || '没有可确认的开放规则。'}</p>
    {!known && excerpt && <details><summary>查看取回的网页片段</summary><p>{excerpt}</p></details>}
    <p>网页规则用于排程核对，不代表实时开放或官方保证。出发前请核实预约与当日公告。</p>
    {fact.retrievedAt && <p>资料获取：{fact.retrievedAt.slice(0, 10)}</p>}
    {source && <a className="item-map-link" href={source} target="_blank" rel="noreferrer">查看来源原文</a>}
  </div></aside>
}
