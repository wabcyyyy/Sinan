import type { ItemActionBody, ItemCreateBody } from '../../types/generated/contracts'
import { recalculate } from './fixtures'
import type { ItineraryDetail } from '../../types/itinerary'
import { moveDemoItem } from './moveItem'

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
    if (index < 0) fixed.push({ dayNo: day.dayNo, name: item.poiName, itemType: item.itemType, startTime: item.startTime.slice(0, 5), endTime: item.endTime.slice(0, 5), cost: null, address: null, remark: null, sourceRef: null })
    return trip
  }
  if (index >= 0) throw new Error('请先解除固定，再修改这条安排')
  if (body.action === 'move') { moveDemoItem(trip, item, body); return recalculate(trip) }
  day.items = day.items.filter(p => p.id !== id)
  if (body.action === 'update') {
    const target = trip.dayList.find(d => d.dayNo === body.dayNo)
    if (!target || !body.startTime || !body.endTime || body.startTime >= body.endTime) throw new Error('请选择日期及有效起止时间')
    if (target.items.some(p => p.startTime && p.endTime && p.startTime.slice(0, 5) < body.endTime! && p.endTime.slice(0, 5) > body.startTime!)) throw new Error('与当天其他安排时间重叠，请调整时段')
    if (body.poiName != null && body.poiName !== item.poiName || body.address != null && body.address !== item.address) {
      item.latitude = null; item.longitude = null; item.poiId = null; item.image = null; item.intro = null; item.description = null; item.whyThis = null; item.openTime = null; item.factEvidence = {}; item.source = 'user'; item.verificationStatus = 'unverified'; item.valueKind = 'estimated'
    }
    if (body.poiName != null) item.poiName = body.poiName
    if (body.address !== undefined) item.address = body.address
    if (body.cost !== undefined) { item.cost = body.cost == null ? null : Number(body.cost); if (item.factEvidence) delete item.factEvidence.cost }
    if (body.remark !== undefined) item.remark = body.remark
    if (item.itemType === 'hotel' && (target.dayNo > trip.stayNights || target.items.some(p => p.itemType === 'hotel'))) throw new Error('该晚已有住宿或不在住宿晚次内')
    item.startTime = body.startTime; item.endTime = body.endTime
    const minutes = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5))
    item.durationMin = minutes(body.endTime) - minutes(body.startTime)
    target.items.push(item); target.items.sort((a, b) => (a.startTime || '').localeCompare(b.startTime || ''))
  }
  if (!day.items.length) throw new Error('请至少保留一条安排')
  for (const changed of trip.dayList.filter(d => d === day || d.dayNo === body.dayNo)) syncSummary(changed)
  return recalculate(trip)
}

function syncSummary(day: ItineraryDetail['dayList'][number]) {
  day.theme = day.items.slice(0, 2).map(p => p.poiName).join(' → ')
  day.note = day.items.map(p => `${p.startTime || ''}–${p.endTime || ''} ${p.poiName}`).join('；')
  delete day.miniRoute
}

export function createDemoItem(source: ItineraryDetail, body: ItemCreateBody): ItineraryDetail {
  const trip = structuredClone(source)
  const day = trip.dayList.find(d => d.dayNo === body.dayNo)
  if (!day || !body.poiName.trim() || !body.startTime || !body.endTime || body.startTime >= body.endTime) throw new Error('请补齐地点和有效时段')
  if (day.items.some(p => p.startTime && p.endTime && p.startTime < body.endTime! && p.endTime > body.startTime!)) throw new Error('与当天其他安排时间重叠，请调整时段')
  if (body.itemType === 'hotel' && (day.dayNo > trip.stayNights || day.items.some(p => p.itemType === 'hotel'))) throw new Error('该晚已有住宿或不在住宿晚次内')
  const id = Math.max(...trip.dayList.flatMap(d => d.items.map(p => p.id || 0))) + 1
  day.items.push({ id, itemType: body.itemType, poiName: body.poiName.trim(), startTime: body.startTime, endTime: body.endTime,
    latitude: body.latitude, longitude: body.longitude,
    cost: body.cost == null ? null : Number(body.cost), address: body.address, remark: body.remark, source: 'user', verificationStatus: 'unverified', valueKind: 'estimated' })
  day.items.sort((a, b) => (a.startTime || '').localeCompare(b.startTime || ''))
  syncSummary(day)
  const next = recalculate(trip)
  return body.fixed ? editDemoItem(next, id, { ...body, action: 'fix', targetIndex: null }) : next
}
