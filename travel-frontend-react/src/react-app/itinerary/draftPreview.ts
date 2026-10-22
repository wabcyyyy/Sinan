import type { ChatDayPlan } from '../../types/chat'
import type { DayPlan } from '../../types/itinerary'
import { structuredDraftChanges } from './chatDraft'
import type { StructuredChange } from './chatDraft'

/** 只读预览：不覆盖现行程，不把缺省字段当成已验证的「保持不变」。 */
export function buildDraftPreview(current: DayPlan[], proposed: ChatDayPlan[], persons: number) {
  const changes = structuredDraftChanges(current, proposed)
  const days = [...new Set(changes.flatMap(change => [change.dayNo, change.fromDay, change.toDay])
    .filter((day): day is number => day != null))]
  const proposedDays = new Set(proposed.map(day => day.day_no))
  const untouchedDays = current.filter(day => !proposedDays.has(day.dayNo)).map(day => day.dayNo)
  const hotelsUntouched = current.some(day => day.items.some(item => item.itemType === 'hotel')) &&
    current.filter(day => day.items.some(item => item.itemType === 'hotel')).every(day => !proposedDays.has(day.dayNo)) &&
    !proposed.some(day => day.items?.some(item => item.item_type === 'hotel'))
  // 仅比较本次替换日的人民币条目小计；住宿变化、缺价或外币都不猜金额。
  let costDelta: number | null = 0
  for (const draft of proposed) {
    const base = current.find(day => day.dayNo === draft.day_no)
    const before = base?.items ?? []
    const after = draft.items ?? []
    const hotelsBefore = before.filter(item => item.itemType === 'hotel')
    const hotelsAfter = after.filter(item => item.item_type === 'hotel')
    const hotelSignature = hotelsBefore.map(item => [item.poiName, item.startTime, item.endTime, item.cost])
    const hotelDraftSignature = hotelsAfter.map(item => [item.poi_name, item.start_time, item.end_time, item.cost])
    const priced = before.every(item => item.cost != null && Number.isFinite(item.cost) && (!item.costCurrency || item.costCurrency === 'CNY')) &&
      after.every(item => item.cost != null && Number.isFinite(item.cost) && (!item.cost_currency || item.cost_currency === 'CNY'))
    if (!base || !draft.items || !priced || JSON.stringify(hotelSignature) !== JSON.stringify(hotelDraftSignature)) {
      costDelta = null
      break
    }
    const oldCost = before.filter(item => item.itemType !== 'hotel').reduce((sum, item) => sum + (item.cost ?? 0), 0)
    const newCost = after.filter(item => item.item_type !== 'hotel').reduce((sum, item) => sum + (item.cost ?? 0), 0)
    costDelta += (newCost - oldCost) * persons
  }
  return {
    changes,
    days: days.length ? days : proposed.map(day => day.day_no),
    untouchedDays,
    hotelsUntouched,
    costDelta: costDelta == null ? null : Math.round(costDelta * 100) / 100,
    added: changes.filter(change => change.type === 'added').reduce((sum, change) => sum + change.items.length, 0),
    removed: changes.filter(change => change.type === 'removed').reduce((sum, change) => sum + change.items.length, 0),
  }
}

export type DraftPreview = ReturnType<typeof buildDraftPreview>

export function itemPreviewMark(changes: StructuredChange[], dayNo: number, name: string, proposed = false) {
  const matches = changes.filter(change => change.items.includes(name.trim()))
  if (matches.some(change => change.type === (proposed ? 'added' : 'removed') && change.dayNo === dayNo)) return proposed ? 'added' : 'removed'
  if (matches.some(change => change.type === 'moved' && (proposed ? change.toDay : change.fromDay) === dayNo)) return 'moved'
  if (matches.some(change => change.type === 'updated' && change.dayNo === dayNo && change.field)) return 'updated'
  return ''
}
