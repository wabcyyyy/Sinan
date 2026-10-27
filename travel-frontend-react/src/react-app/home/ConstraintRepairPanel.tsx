import { useState } from 'react'
import { reviseConstraintRequirements } from '../../api/sinan'
import type { TripRequirements } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'

export function ConstraintRepairPanel({ trip, requirements, onAdopt }: {
  trip: ItineraryDetail; requirements: TripRequirements; onAdopt: (requirements: TripRequirements) => void
}) {
  const [error, setError] = useState('')
  const [working, setWorking] = useState(false)
  const reports = trip.dayList.flatMap(d => d.constraintPlanning?.status === 'INFEASIBLE' ? [d.constraintPlanning] : [])
  if (!reports.length) return null
  const suggestions = [...new Map(reports.flatMap(r => r.repairSuggestions || []).map(s => [`${s.action}:${s.constraintId}`, s])).values()]
  const conflicts = [...new Set(reports.flatMap(r => r.report.checks.map(c => c.reason)))]
  return <section className="scenario-content" aria-label="调整冲突要求">
    <h3>这些要求暂时无法同时安排</h3>
    <ul>{conflicts.map(reason => <li key={reason}>{reason}</li>)}</ul>
    <p>可以选择下面一项修订，回到需求确认后重新规划；原行程与固定预约会保留。</p>
    {suggestions.map(s => <div key={`${s.action}:${s.constraintId}`}>
      {s.sourceText && <q>{s.sourceText}</q>}
      <button type="button" className="button button-secondary" disabled={working} onClick={async () => {
        if (trip.planningRevision === undefined) { setError('请刷新后重新核对版本'); return }
        setWorking(true)
        try {
          const next = await reviseConstraintRequirements(trip.id, { planningRevision: trip.planningRevision, constraintId: s.constraintId, action: s.action })
          const confirmedSource = next.requirements.requirementSources.at(-1)
          const sources = confirmedSource?.target === 'confirmed_repair' ? [...(requirements.requirementSources || []), confirmedSource].slice(-60) : requirements.requirementSources || []
          if (s.action === 'soften_relation') {
            const revised = next.requirements.placeRelations.find(r => r.constraintId === s.constraintId)
            const current = requirements.placeRelations.find(r => r.constraintId === s.constraintId)
            if (!revised || !current || current.kind !== revised.kind || current.dayNo !== revised.dayNo || current.placeNames.join('\n') !== revised.placeNames.join('\n')) throw new Error('关系要求已变化，请先按新需求规划')
            onAdopt({ ...requirements, requirementSources: sources, placeRelations: requirements.placeRelations.map(r => r === current ? revised : r) })
          } else {
            const current = requirements.requiredPlaces.find(p => p.constraintId === s.constraintId)
            if (!current || requirements.fixedArrangements.some(f => f.name === current.name)) throw new Error('必去或预约已变化，请先按新需求规划')
            onAdopt({ ...requirements, requirementSources: sources, requiredPlaces: requirements.requiredPlaces.filter(p => p !== current) })
          }
        }
        catch (e) { setError(e instanceof Error ? e.message : '修订未采用') }
        finally { setWorking(false) }
      }}>采用修订：{s.label}</button>
    </div>)}
    {!suggestions.length && <p>请在需求表单或对话中调整冲突的日期、预算或预约；系统不会自动取消固定安排。</p>}
    {error && <p role="alert">{error}</p>}
  </section>
}
