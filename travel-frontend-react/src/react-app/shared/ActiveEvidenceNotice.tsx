import type { ActiveEvidenceReport } from '../../types/generated/contracts'

const stops: Record<string, string> = {
  no_remaining_gap: '本轮已无可查缺口', cost_budget: '达到查询额度', query_budget: '达到查询次数上限',
  decision_stable: '本轮单字段试解未改变可行性或选择', probe_unknown: '部分试解未确认结果',
  awaiting_user: '需要你补充资料', time_or_run_budget: '达到时间或运行预算', disabled: '未启用',
}
const outcomes: Record<string, string> = { updated: '已采用', rejected: '未采用', error: '查询失败', ask_user: '待补充', unknown: '未确认' }

export function ActiveEvidenceNotice({ report }: { report?: ActiveEvidenceReport | null }) {
  if (!report) return null
  return <details className="scenario-content">
    <summary>本轮资料核实 · {report.queries.length} 项 · {stops[report.stopReason] || '已停止'}</summary>
    <p>优先补查可能影响可行性、地点选择或顺序的资料。其余未确认事实仍保留，出发前需核实。</p>
    {report.questions.length > 0 && <ul>{report.questions.map((q, i) => <li key={i}>{q}</li>)}</ul>}
    <ul>{report.queries.map((q, i) => <li key={i}>
      {outcomes[q.outcome]}：{q.reason}{q.decisionChanged && '；已改变可行性或选择'}
      {q.retrievedAt && ` · 获取于 ${q.retrievedAt}`}
      {q.sourceUrl && /^https?:\/\//.test(q.sourceUrl) && <> · <a href={q.sourceUrl} target="_blank" rel="noreferrer">查看来源</a></>}
    </li>)}</ul>
    <p>仅比较单项资料的两种假设，未变化不代表所有资料都已可靠。核实停止后仍按当前事实重新判断。</p>
  </details>
}
