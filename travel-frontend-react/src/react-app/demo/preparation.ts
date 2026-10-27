import type { PreparationPutBody, PreparationVO } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'

export function emptyDemoPreparation(): PreparationVO {
  return { revision: 0, data: { places: [], bookings: [], tasks: [], confirmations: [], completedItemIds: [] }, updatedAt: null }
}

export function saveDemoPreparation(previous: PreparationVO, body: PreparationPutBody, trip: ItineraryDetail): PreparationVO {
  if (body.revision !== previous.revision) throw new Error('准备资料已在另一处更新，请重新载入后再修改')
  const data = structuredClone(body.data)
  const items = trip.dayList.flatMap(d => d.items)
  for (const entry of data.confirmations) {
    const old = previous.data.confirmations.find(c => c.id === entry.id)
    if (old && old.itemId === entry.itemId && old.name === entry.name && old.aspect === entry.aspect && old.note === entry.note && old.sourceUrl === entry.sourceUrl) entry.recordedAt = old.recordedAt
    else {
      if (!items.some(p => p.id === entry.itemId && p.poiName === entry.name)) throw new Error('核实对象已变化，请重新选择')
      entry.recordedAt = new Date().toISOString()
    }
  }
  return { revision: previous.revision + 1, data, updatedAt: new Date().toISOString() }
}
