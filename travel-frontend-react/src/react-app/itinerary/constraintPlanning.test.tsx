import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ConstraintPlanningAssessment } from '../../types/generated/contracts'
import { ConstraintPlanningNotice } from './ConstraintPlanningNotice'
import { destinationDate } from './TravelToday'
import { formatMoney } from '../../shared/money'

function assessment(overrides: Partial<ConstraintPlanningAssessment> = {}): ConstraintPlanningAssessment {
  return {
    status: 'UNKNOWN', solverStatus: 'OPTIMAL', numericalStatus: 'OPTIMAL', planningRevision: 1, isCurrent: true,
    conditional: true, scope: 'single_city_trip', localDate: '2026-10-25', destinationSource: 'user_confirmed',
    destination: { countryCode: 'FR', timeZone: 'Europe/Paris', currency: 'EUR', activeEvidence: null }, activeEvidence: null,
    budgetScope: '仅已列活动费用', report: { checks: [{ constraintId: 'price:p1', kind: 'planning_evidence',
      status: 'unknown', dayNo: 1, itemRefs: ['地点'], reason: '费用尚未核实', evidenceRefs: [], blocking: true }] },
    conflictIds: [], conflictIrreducible: false, repairSuggestions: [], routes: [], facts: [], knownCost: 10, totalCost: null,
    solveSeconds: 0.1, optimizedTiers: 3, totalTiers: 3, objectiveBound: 0, reason: '事实待核实', ...overrides,
  }
}

describe('constraint planning disclosure', () => {
  it('separates numerical feasibility from unverified facts and original currencies', () => {
    const html = renderToStaticMarkup(<ConstraintPlanningNotice assessment={assessment()} />)
    expect(html).toContain('当前假设下有可行安排')
    expect(html).toContain('费用尚未核实')
    expect(html).toContain('无已确认汇率时不合计')
    expect(html).toContain('Europe/Paris')
    expect(html).not.toContain('所有事实已核实')
  })
  it('shows unanswered evidence and stopping reason without calling it confirmed', () => {
    const html = renderToStaticMarkup(<ConstraintPlanningNotice assessment={assessment({ activeEvidence: {
      policy: 'bottleneck_heuristic', probes: [], queries: [], questions: ['请确认具体馆区的开放时段'],
      spentUnits: 2, budgetUnits: 4, stopReason: 'awaiting_user', solveCalls: 3, elapsedSeconds: .1, assumptions: [],
      retrievalCalls: 0, llmCalls: 0, promptTokens: 0, completionTokens: 0,
    } })} />)
    expect(html).toContain('需要你补充资料')
    expect(html).toContain('请确认具体馆区的开放时段')
    expect(html).toContain('未变化不代表所有资料都已可靠')
  })
  it('does not call an incumbent or stale assessment optimal', () => {
    const feasible = renderToStaticMarkup(<ConstraintPlanningNotice assessment={assessment({ numericalStatus: 'FEASIBLE', optimizedTiers: 0 })} />)
    expect(feasible).toContain('最优性尚未证明')
    expect(feasible).not.toContain('已完成各目标层求解')
    const stale = renderToStaticMarkup(<ConstraintPlanningNotice assessment={assessment({ isCurrent: false })} />)
    expect(stale).toContain('原排程结论待重新检查')
    expect(stale).not.toContain('已完成各目标层求解')
  })
  it('uses destination dates across UTC midnight and retains currency units', () => {
    const instant = new Date('2026-10-24T16:30:00Z')
    expect(destinationDate('Asia/Tokyo', instant)).toBe('2026-10-25')
    expect(destinationDate('Europe/Paris', instant)).toBe('2026-10-24')
    expect(formatMoney(1200, 'JPY')).toContain('JPY')
    expect(formatMoney(10, 'EUR')).toContain('EUR')
  })
  it('shows static network provenance, attribution and map correction for OSRM', () => {
    const html = renderToStaticMarkup(<ConstraintPlanningNotice assessment={assessment({ routes: [{
      fromId: 'a', toId: 'b', mode: 'walking', source: 'osrm', basis: 'road_network', durationMin: 15,
      sourceUrl: 'https://routing.openstreetmap.de', attribution: 'OSRM / FOSSGIS; © OpenStreetMap contributors',
      fetchedAt: '2026-10-24T09:00:00Z', dataDate: null, timeDependent: false,
    }] })} />)
    expect(html).toContain('静态路网估算，非实时交通')
    expect(html).toContain('OSRM / FOSSGIS')
    expect(html).toContain('https://www.openstreetmap.org/fixthemap')
    expect(html).toContain('https://routing.openstreetmap.de')
  })
})
