import type { TripRequirements } from '../../types/generated/contracts'

/** 展示服务端实际提取的要求，不从原话再推断一份约束。 */
export function RequirementSummary({ requirements: r }: { requirements?: TripRequirements | null }) {
  if (!r) return null
  const lines: string[] = []
  for (const f of r.fixedArrangements ?? []) lines.push(`第 ${f.dayNo} 天固定：${f.name} ${f.startTime}–${f.endTime}${f.cost != null ? ` · 用户填写单价 ${r.constraintPlanning?.currency || 'CNY'} ${f.cost}` : ''}`)
  for (const place of r.requiredPlaces ?? []) lines.push(`${place.dayNo ? `第 ${place.dayNo} 天` : '全程'}必去：${place.name}`)
  for (const relation of r.placeRelations ?? []) lines.push(`第 ${relation.dayNo} 天${relation.strength === 'soft' ? '优先' : '要求'}：${relation.kind === 'before' ? `${relation.placeNames[0]} 之后再去 ${relation.placeNames[1]}` : `${relation.placeNames.join('、')} 至少安排一处`}`)
  if (r.excludedPlaces?.length) lines.push(`避开：${r.excludedPlaces.join('、')}`)
  const categories: Record<string, string> = { temple: '寺庙/神社', museum: '博物馆' }
  if (r.excludedCategories?.length) lines.push(`不安排：${r.excludedCategories.map((v) => categories[v] ?? v).join('、')}`)
  for (const window of r.dayWindows ?? []) {
    const times = [window.notBefore && `${window.notBefore} 后开始`, window.finishBy && `${window.finishBy} 前结束`].filter(Boolean)
    if (times.length) lines.push(`第 ${window.dayNo} 天${window.kind === 'arrival' ? '（到达日）' : window.kind === 'departure' ? '（离开日）' : ''}：${times.join('，')}`)
  }
  if (r.pace === 'relaxed') lines.push('节奏轻松')
  if (r.maxAttractionsPerDay != null) lines.push(`每天最多 ${r.maxAttractionsPerDay} 个景点`)
  if (r.dietaryRequirement) lines.push(`${r.dietaryRequirement === 'vegan' ? '纯素（不含蛋奶）' : '素食（可含蛋奶）'} · ${r.diningEvidencePolicy === 'review_candidates' ? '餐饮候选需自行核实' : '只安排有饮食依据的餐厅'}`)
  const transport = { walking: '步行为主', driving: '自驾为主', mixed: '混合交通', unspecified: '' }
  if (r.transportPreference && transport[r.transportPreference]) lines.push(transport[r.transportPreference])
  if (r.maxWalkMinutesPerLeg != null) lines.push(`单段步行最多 ${r.maxWalkMinutesPerLeg} 分钟（估算，需核实路线）`)
  if (r.budgetPolicy?.mode) lines.push(r.budgetPolicy.mode === 'hard_cap' ? '预算为上限（按估算费用校验）' : '预算为目标参考')
  if (r.budgetPolicy?.includeIntercityTransport != null) lines.push(r.budgetPolicy.includeIntercityTransport ? '预算包含城际交通' : '预算不含城际交通')
  if (r.lodging?.rooms) lines.push(`住宿 ${r.lodging.rooms} 间`)
  if (r.lodging?.lockedHotelIdentity) lines.push(`保留酒店：${r.lodging.lockedHotelIdentity}`)
  if (!lines.length && !r.unresolvedRequests?.length) return null
  return <section className="requirement-summary" aria-label="明确要求">
    <h4>这次规划会带上这些要求</h4>
    <ul>{lines.map((line, index) => <li key={`${index}-${line}`}>{line}</li>)}</ul>
    {Boolean(r.requirementSources?.length) && <details><summary>核对原话与提取结果</summary><ul>{r.requirementSources.map((source, index) => <li key={index}><q>{source.sourceText}</q> · {source.disposition === 'mapped' ? '已记录在要求中' : '需要继续确认'}</li>)}</ul></details>}
    {Boolean(r.unresolvedRequests?.length) && <p role="status">尚未支持或需要澄清：{r.unresolvedRequests.join('；')}</p>}
    <p>有遗漏？在对话里补充后再开始。开放、预约与交通时间仍需出发前核实。</p>
  </section>
}
