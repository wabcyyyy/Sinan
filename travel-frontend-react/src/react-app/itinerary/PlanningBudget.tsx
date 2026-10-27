import type { ItineraryDetail } from '../../types/itinerary'
import { formatMoney } from '../../shared/money'

const kinds: Record<string, string> = { attraction: '门票', food: '餐饮', hotel: '住宿', transport: '交通', activity: '体验' }

export function PlanningBudget({ trip }: { trip: ItineraryDetail }) {
  const items = trip.dayList.flatMap(d => d.items)
  const currency = trip.budgetCurrency || 'CNY'
  const strict = trip.dayList.some(d => d.constraintPlanning)
  const unknown = items.filter(p => p.cost == null)
  const excluded = strict ? items.filter(p => p.cost != null && p.costCurrency !== currency) : []
  return <section className="planning-budget" aria-label="规划预算分解">
    <h3>把预算花在哪里</h3>
    <div className="prep-budget-total"><span>已列同币项目的规划合计<strong>{formatMoney(trip.totalAmount, currency)}</strong></span><span>预算上限<strong>{trip.budget == null ? '未设置' : formatMoney(trip.budget, currency)}</strong></span></div>
    <p className="prep-caption">规划估算，非实付账本、实时报价或供应商账单。未知费用未计入合计；用户填写金额也需要核对计价口径。</p>
    <table><caption>当前行程的分类估算（{currency}）</caption><thead><tr><th>类别</th><th>项目数</th><th>参考总额</th></tr></thead><tbody>{trip.budgetList.map(row => <tr key={row.category}><td>{row.category}</td><td>{row.itemCount}</td><td>{formatMoney(row.amount, currency)}</td></tr>)}</tbody></table>
    <details><summary>逐项单价与人数／房晚口径</summary>{trip.dayList.map(day => <div key={day.dayNo}><h4>第 {day.dayNo} 天</h4>{day.items.map((p, i) => <p key={p.id ?? i} className="prep-budget-row"><span>{p.poiName}<small>{kinds[p.itemType] || '其他'} · {p.source === 'user' ? '用户填写' : '规划参考'}{p.itemType === 'hotel' ? ' · 每间晚，按行程房间数' : ` · 每人，${trip.persons} 人`}</small></span><strong>{p.cost == null ? '待补充' : strict && !p.costCurrency ? `${p.cost}（币种待确认）` : formatMoney(p.cost, p.costCurrency || currency)}</strong></p>)}</div>)}</details>
    {unknown.length > 0 && <p className="prep-unknown">还有 {unknown.length} 项未知费用，不能当作免费：{unknown.map(p => p.poiName).join('、')}</p>}
    {excluded.length > 0 && <p className="prep-unknown">{excluded.length} 项异币或币种未确认，保留原值且未计入合计：{excluded.map(p => p.poiName).join('、')}。没有已确认的汇率记录，不进行换算。</p>}
    <p className="prep-caption">城市间交通、未列出的市内交通及其他自选消费可能尚未包含。调整单价请在相应行程条目中编辑。</p>
  </section>
}
