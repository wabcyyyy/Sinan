import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { reviseConstraintRequirements } from '../../api/sinan'
import type { ConstraintPlanningAssessment, TripRequirements } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { ConstraintRepairPanel } from './ConstraintRepairPanel'

vi.mock('../../api/sinan', async original => ({ ...await original<typeof import('../../api/sinan')>(), reviseConstraintRequirements: vi.fn() }))
const requirements: TripRequirements = {
  factSource: null, travelPlanner: 'generate', travelConstraints: null,
  schemaVersion: 1, dayWindows: [], requiredPlaces: [], fixedArrangements: [], placeInputs: [], excludedPlaces: [], excludedCategories: [],
  pace: null, maxAttractionsPerDay: null, dietaryRequirement: null, diningEvidencePolicy: null, transportPreference: null,
  maxWalkMinutesPerLeg: null, budgetPolicy: null, lodging: null, unresolvedRequests: [], requirementSources: [], constraintPlanning: null,
  placeRelations: [{ constraintId: 'order', kind: 'before', placeNames: ['A', 'B'], dayNo: 1, strength: 'hard', priority: 1, negotiable: false, sourceText: '先A后B', sourceTurnId: null }],
}
const assessment: ConstraintPlanningAssessment = {
  status: 'INFEASIBLE', solverStatus: 'INFEASIBLE', numericalStatus: 'INFEASIBLE', planningRevision: 2, isCurrent: true,
  conditional: true, scope: 'single_city_trip', localDate: '2026-10-25', destinationSource: 'user_confirmed',
  destination: { countryCode: 'FR', currency: 'EUR', timeZone: 'Europe/Paris', activeEvidence: null }, budgetScope: '活动',
  report: { checks: [] }, conflictIds: ['order'], conflictIrreducible: true, routes: [], facts: [], knownCost: 0,
  totalCost: null, solveSeconds: 0, optimizedTiers: 0, totalTiers: 0, objectiveBound: null, reason: '关系冲突', activeEvidence: null,
  repairSuggestions: [{ constraintId: 'order', action: 'soften_relation', label: '将先后改为偏好', sourceText: '先A后B' }],
}
const trip: ItineraryDetail = { id: 7, title: 'Paris', city: 'Paris', days: 1, persons: 1, status: 3, planningRevision: 2,
  stayNights: 0, budgetList: [], totalAmount: 0,
  requirementsStruct: requirements, dayList: [{ dayNo: 1, dayId: 1, items: [], constraintPlanning: assessment }] }
afterEach(() => { vi.clearAllMocks(); document.body.innerHTML = '' })
beforeEach(() => { (globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true })

it('用户选择后服务端核对版本，只将选中修订放回需求确认，不启动生成', async () => {
  const revised = { ...requirements, requirementSources: [{ sourceText: '用户确认修订：将先后改为偏好', target: 'confirmed_repair', disposition: 'mapped' as const }], placeRelations: requirements.placeRelations.map(r => ({ ...r, strength: 'soft' as const })) }
  vi.mocked(reviseConstraintRequirements).mockResolvedValue({ requirements: revised, baseRevision: 2, notice: '待确认' })
  const adopt = vi.fn()
  const container = document.createElement('div'); document.body.appendChild(container)
  const root = createRoot(container)
  await act(async () => root.render(<ConstraintRepairPanel trip={trip} requirements={requirements} onAdopt={adopt} />))
  expect(adopt).not.toHaveBeenCalled()
  await act(async () => container.querySelector('button')!.click())
  expect(reviseConstraintRequirements).toHaveBeenCalledWith(7, { planningRevision: 2, constraintId: 'order', action: 'soften_relation' })
  expect(adopt).toHaveBeenCalledWith(revised)
  expect(requirements.placeRelations[0].strength).toBe('hard')
  await act(async () => root.unmount())
})

it('过期修订不采用，保留当前需求并显示错误', async () => {
  vi.mocked(reviseConstraintRequirements).mockRejectedValue(new Error('版本已变化'))
  const adopt = vi.fn(); const container = document.createElement('div'); document.body.appendChild(container)
  const root = createRoot(container)
  await act(async () => root.render(<ConstraintRepairPanel trip={trip} requirements={requirements} onAdopt={adopt} />))
  await act(async () => container.querySelector('button')!.click())
  expect(adopt).not.toHaveBeenCalled()
  expect(container.textContent).toContain('版本已变化')
  await act(async () => root.unmount())
})
