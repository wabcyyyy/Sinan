import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { actOnItem, compareReplanning, parseReplanChange, selectReplan } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { ItemActions } from './ItemActions'
import { ReplanOptions, replanChange } from './ReplanOptions'
import { ReplanComposer } from './ReplanComposer'

vi.mock('../../api/sinan', async importOriginal => ({
  ...await importOriginal<typeof import('../../api/sinan')>(),
  actOnItem: vi.fn(), compareReplanning: vi.fn(), parseReplanChange: vi.fn(), selectReplan: vi.fn(),
}))

const assessment: Contracts.ConstraintPlanningAssessment = {
  status: 'UNKNOWN', solverStatus: 'OPTIMAL', numericalStatus: 'OPTIMAL', planningRevision: 4, isCurrent: true,
  conditional: true, scope: 'single_city_trip', localDate: '2026-10-25', destinationSource: 'user_confirmed',
  destination: { countryCode: 'FR', timeZone: 'Europe/Paris', currency: 'EUR', activeEvidence: null }, activeEvidence: null, budgetScope: '已列活动',
  report: { checks: [] }, conflictIds: [], conflictIrreducible: false, repairSuggestions: [], routes: [], facts: [], knownCost: 0,
  totalCost: null, solveSeconds: 0, optimizedTiers: 3, totalTiers: 3, objectiveBound: null, reason: '事实待核实',
}

const trip: ItineraryDetail = {
  id: 7, title: 'Paris', city: 'Paris', days: 1, stayNights: 0, persons: 1, budget: 100, status: 2, planningRevision: 4,
  dayList: [{ dayId: 1, dayNo: 1, constraintPlanning: assessment, executedItemIds: [], items: [
    { id: 11, poiName: 'A', itemType: 'attraction', startTime: '09:00', endTime: '10:00' },
    { id: 12, poiName: 'B', itemType: 'attraction', startTime: '10:20', endTime: '11:20' },
  ] }], budgetList: [], totalAmount: 0,
}

function comparison(options = true): Contracts.ReplanCompareVO {
  return {
    comparisonId: options ? 'comparison-1' : null, planningRevision: 4, conflicts: options ? [] : ['固定预约与新要求冲突'],
    attempts: [{ scope: 'local', status: options ? 'OPTIMAL' : 'INFEASIBLE', movableItemIds: [11, 12], reason: '受影响区段', seconds: 0.1 }],
    dependencies: [], assumptions: [], activeEvidence: null, options: options ? [{ key: 'minimal', title: '优先减少改动', numericalStatus: 'OPTIMAL',
      factStatus: 'UNKNOWN', changedItemIds: [12], affectedDayNos: [1], reasons: ['保留固定项'], signature: 'schedule-a',
      metrics: { removedItems: 0, crossDayMoves: 0, changedItems: 1, orderChanges: 1, startShiftMinutes: 80,
        travelMinutes: 20, meanStartShiftMinutes: 40, lockedViolations: 0 } }] : [],
  }
}

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.clearAllMocks()
  ;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  vi.mocked(compareReplanning).mockResolvedValue(comparison())
  vi.mocked(selectReplan).mockResolvedValue({ messageId: 5, baseRevision: 'base-4' })
})
afterEach(async () => { await act(async () => root.unmount()); host.remove() })

async function enter(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
}

describe('P2 compare before save', () => {
  it('moves by stable item IDs and revision, then selects a draft without direct item writes', async () => {
    const changed = vi.fn(), draftCreated = vi.fn()
    await act(async () => root.render(<ItemActions trip={trip} day={trip.dayList[0]} item={trip.dayList[0].items[1]} disabled={false} onChanged={changed} onDraftCreated={draftCreated} />))
    const move = Array.from(host.querySelectorAll('button')).find(b => b.textContent === '↑ 上移')!
    await act(async () => move.click())
    expect(compareReplanning).toHaveBeenCalledWith(7, expect.objectContaining({ planningRevision: 4,
      change: expect.objectContaining({ kind: 'move', itemId: 12, anchorItemId: 11, placement: 'before', dayNo: 1 }) }))
    expect(actOnItem).not.toHaveBeenCalled()
    expect(changed).not.toHaveBeenCalled()
    expect(host.textContent).toContain('当前行程尚未修改')
    await act(async () => Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('选此方案'))!.click())
    expect(selectReplan).toHaveBeenCalledWith(7, { planningRevision: 4, comparisonId: 'comparison-1', choice: 'minimal' })
    expect(draftCreated).toHaveBeenCalledOnce()
    expect(changed).not.toHaveBeenCalled()
  })

  it('shows conflicts without an apply action and keeps conditional facts visible', () => {
    const html = renderToStaticMarkup(<ReplanOptions trip={trip} result={comparison(false)} onSelected={() => {}} />)
    expect(html).toContain('固定预约与新要求冲突')
    expect(html).not.toContain('选此方案')
    const feasible = renderToStaticMarkup(<ReplanOptions trip={trip} result={comparison()} onSelected={() => {}} />)
    expect(feasible).toContain('部分事实仍待核实')
    expect(feasible).toContain('开始时间偏移')
    expect(feasible).toContain('80 分钟')
  })

  it('compares an edited time span and accepts an unchanged numeric fee entered as text', async () => {
    const item = { ...trip.dayList[0].items[1], cost: 10, costCurrency: 'EUR' }
    const feeTrip = { ...trip, budgetCurrency: 'EUR', dayList: [{ ...trip.dayList[0], items: [trip.dayList[0].items[0], item] }] }
    const changed = vi.fn()
    await act(async () => root.render(<ItemActions trip={feeTrip} day={feeTrip.dayList[0]} item={item} disabled={false} onChanged={changed} />))
    await act(async () => Array.from(host.querySelectorAll('button')).find(b => b.textContent === '编辑安排')!.click())
    expect(host.textContent).toContain('单价（EUR/人）')
    const fee = host.querySelector<HTMLInputElement>('input[type="number"]')!
    await enter(fee, '11')
    await enter(fee, '10')
    await enter(host.querySelectorAll<HTMLInputElement>('input[type="time"]')[1], '11:10')
    await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    expect(compareReplanning).toHaveBeenCalledWith(7, expect.objectContaining({ change: expect.objectContaining({
      kind: 'move', itemId: 12, dayNo: 1, startTime: '10:20', endTime: '11:10',
    }) }))
    expect(actOnItem).not.toHaveBeenCalled()
    expect(changed).not.toHaveBeenCalled()
  })

  it('routes removal through a close comparison and preserves original foreign fee labels', async () => {
    const item = { ...trip.dayList[0].items[1], cost: 10, costCurrency: 'USD' }
    const feeTrip = { ...trip, budgetCurrency: 'EUR', dayList: [{ ...trip.dayList[0], items: [trip.dayList[0].items[0], item] }] }
    await act(async () => root.render(<ItemActions trip={feeTrip} day={feeTrip.dayList[0]} item={item} disabled={false} onChanged={() => {}} />))
    await act(async () => Array.from(host.querySelectorAll('button')).find(b => b.textContent === '编辑安排')!.click())
    expect(host.textContent).toContain('单价（USD/人）')
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')!.disabled).toBe(true)
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')!.value).toBe('10')
    await act(async () => Array.from(host.querySelectorAll('button')).find(b => b.textContent === '移除')!.click())
    await act(async () => Array.from(host.querySelectorAll('button')).find(b => b.textContent === '比较移除方案')!.click())
    expect(compareReplanning).toHaveBeenCalledWith(7, expect.objectContaining({ change: expect.objectContaining({ kind: 'close', itemId: 12 }) }))
    expect(actOnItem).not.toHaveBeenCalled()
  })

  it('provides the same operation body for rule parsed text and preserves explicit completed scope', async () => {
    const operation = { ...replanChange('delay', 12), delayMinutes: 30 }
    vi.mocked(parseReplanChange).mockResolvedValue({ change: operation, questions: [], parser: 'bounded_rules' })
    const completedTrip = { ...trip, dayList: [{ ...trip.dayList[0], executedItemIds: [11] }] }
    await act(async () => root.render(<ReplanComposer trip={completedTrip} dayNo={1} selectedItemId={12} onSelected={() => {}} />))
    const input = host.querySelector<HTMLInputElement>('input[maxlength="2000"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '推迟30分钟')
      input.dispatchEvent(new Event('input', { bubbles: true }))
      input.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await act(async () => Array.from(host.querySelectorAll('button')).find(b => b.textContent === '理解并比较')!.click())
    expect(parseReplanChange).toHaveBeenCalledWith(7, { planningRevision: 4, dayNo: 1, itemId: 12, message: '推迟30分钟' })
    expect(compareReplanning).toHaveBeenCalledWith(7, { planningRevision: 4, change: operation,
      completedItemIds: [11], optionalItemIds: [], allowCrossDay: true })
    expect(actOnItem).not.toHaveBeenCalled()
  })
})
