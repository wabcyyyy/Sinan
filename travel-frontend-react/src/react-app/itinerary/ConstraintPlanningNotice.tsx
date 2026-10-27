import type { ConstraintPlanningAssessment } from '../../types/generated/contracts'
import { ActiveEvidenceNotice } from '../shared/ActiveEvidenceNotice'

const numerical: Record<string, string> = {
  OPTIMAL: '当前假设下有可行安排，已完成各目标层求解', FEASIBLE: '当前假设下有可行安排，最优性尚未证明',
  INFEASIBLE: '当前候选及假设下不可行', UNKNOWN: '时限内未确认能否排出', NOT_RUN: '资料不足，尚未求解',
}

export function ConstraintPlanningNotice({ assessment }: { assessment?: ConstraintPlanningAssessment | null }) {
  if (!assessment) return null
  const gaps = assessment.report.checks.filter(c => c.status === 'unknown')
  const labels = new Map(assessment.facts.map(f => [f.candidateId, f.name]))
  return <section className="scenario-content" aria-label="时间约束规划说明">
    <h3>时间约束规划</h3>
    <p role="status">{assessment.isCurrent ? numerical[assessment.numericalStatus] || '求解状态待确认' : '安排已更新或仍在生成，原排程结论待重新检查'}</p>
    <p>{assessment.localDate} · {assessment.destination.timeZone} · {assessment.destination.countryCode} · 计价 {assessment.destination.currency}</p>
    <p>以上结论基于当前候选和数据假设。营业、交通和价格是否核实另行判断，出发前仍需确认。</p>
    {assessment.status === 'UNKNOWN' && <p className="fact-warning">事实或需求仍待确认：{assessment.reason}</p>}
    {assessment.conflictIds.length > 0 && <div><h4>冲突涉及的要求</h4><ul>{assessment.report.checks.map(c => <li key={c.constraintId}>{c.itemRefs.join('、')}：{c.reason}</li>)}</ul><p>{assessment.conflictIrreducible ? '已检查逐条删除；不代表这是约束数量最少的集合。' : '这是充分冲突集合，尚未证明不可约。'}用户确认前不会自动放宽要求。</p></div>}
    {gaps.length > 0 && <details><summary>{gaps.length} 项待补充或核实</summary><ul>{gaps.map(c => <li key={c.constraintId}>{c.itemRefs.join('、')}：{c.reason}</li>)}</ul></details>}
    <p>{assessment.budgetScope}。其他币种金额保留原值，无已确认汇率时不合计。</p>
    <ActiveEvidenceNotice report={assessment.activeEvidence} />
    {assessment.routes.length > 0 && <details><summary>交通依据</summary><ul>{assessment.routes.map(r => <li key={`${r.fromId}:${r.toId}`}>
      {labels.get(r.fromId) || r.fromId} → {labels.get(r.toId) || r.toId}：{r.durationMin == null ? '时长未知' : `约 ${r.durationMin} 分钟`} · {r.basis === 'road_network' ? '静态路网估算，非实时交通' : r.basis === 'coordinate_estimate' ? '坐标估算，未取得真实路线' : '路线来源未确认'}
      {r.fetchedAt && ` · 查询于 ${r.fetchedAt}`}{r.dataDate && ` · 路网数据 ${r.dataDate}`}
      {r.attribution && <span> · {r.attribution} · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">数据许可</a> · <a href="https://www.openstreetmap.org/fixthemap" target="_blank" rel="noreferrer">修正地图</a></span>}
      {r.sourceUrl && /^https?:\/\//.test(r.sourceUrl) && <> · <a href={r.sourceUrl} target="_blank" rel="noreferrer">路线来源</a></>}
    </li>)}</ul></details>}
    <details><summary>候选资料来源</summary><ul>{assessment.facts.map(f => <li key={f.candidateId}>{f.name} · {f.sourceLanguage || '来源语言未注明'} · {f.source} {f.sourceUrl && /^https?:\/\//.test(f.sourceUrl) && <a href={f.sourceUrl} target="_blank" rel="noreferrer">查看来源</a>}{f.retrievedAt && ` · 获取于 ${f.retrievedAt}`}</li>)}</ul></details>
  </section>
}
