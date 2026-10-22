import type { ItemActionBody } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'

/** 纯手动编辑样例数据，不模拟 AI、地图验证或预订能力。 */
export function editDemoItem(source: ItineraryDetail, id: number, body: ItemActionBody): ItineraryDetail {
  const trip = structuredClone(source)
  const day = trip.dayList.find(d => d.items.some(p => p.id === id))
  const item = day?.items.find(p => p.id === id)
  if (!day || !item) throw new Error('演示条目不存在')
  const fixed = (trip.fixedArrangements ||= [])
  const index = fixed.findIndex(f => f.dayNo === day.dayNo && f.name === item.poiName && f.itemType === item.itemType)
  if (body.action === 'unfix') { if (index >= 0) fixed.splice(index, 1); return trip }
  if (body.action === 'fix') {
    if (!item.startTime || !item.endTime || item.endTime <= item.startTime) throw new Error('请先补齐有效起止时间')
    if (index < 0) fixed.push({ dayNo: day.dayNo, name: item.poiName, itemType: item.itemType, startTime: item.startTime.slice(0, 5), endTime: item.endTime.slice(0, 5) })
    return trip
  }
  if (index >= 0) throw new Error('请先解除固定，再修改这条安排')
  if (item.itemType === 'hotel') throw new Error('住宿请使用住宿调整入口')
  day.items = day.items.filter(p => p.id !== id)
  if (body.action === 'update') {
    const target = trip.dayList.find(d => d.dayNo === body.dayNo)
    if (!target || !body.startTime || !body.endTime || body.startTime >= body.endTime) throw new Error('请选择日期及有效起止时间')
    if (target.items.some(p => p.startTime && p.endTime && p.startTime.slice(0, 5) < body.endTime! && p.endTime.slice(0, 5) > body.startTime!)) throw new Error('与当天其他安排时间重叠，请调整时段')
    item.startTime = body.startTime; item.endTime = body.endTime
    const minutes = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5))
    item.durationMin = minutes(body.endTime) - minutes(body.startTime)
    target.items.push(item); target.items.sort((a, b) => (a.startTime || '').localeCompare(b.startTime || ''))
  }
  if (!day.items.length) throw new Error('请至少保留一条安排')
  return trip
}
