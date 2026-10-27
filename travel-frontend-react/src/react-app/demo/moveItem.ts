import type { ItineraryDetail, TripItem } from '../../types/itinerary'
import type { ItemActionBody } from '../../types/generated/contracts'

/** 演示排序保留时长与间隔，固定项不参与重排。 */
export function moveDemoItem(trip: ItineraryDetail, item: TripItem, body: ItemActionBody) {
  const day = trip.dayList.find(d => d.items.includes(item))!
  const target = trip.dayList.find(d => d.dayNo === body.dayNo)
  if (!target || !item.startTime || !item.endTime) throw new Error('请选择日期并补齐有效时段')
  if (target !== day) {
    if (target.items.some(p => p.startTime && p.endTime && p.startTime < item.endTime! && p.endTime > item.startTime!)) throw new Error('目标天的时间重叠，请改用编辑安排调整时段')
    if (item.itemType === 'hotel' && (target.dayNo > trip.stayNights || target.items.some(p => p.itemType === 'hotel'))) throw new Error('该晚已有住宿或不在住宿晚次内')
    if (day.items.length === 1) throw new Error('请至少保留一条安排')
    day.items = day.items.filter(p => p !== item)
    target.items.push(item)
    target.items.sort((a, b) => (a.startTime || '').localeCompare(b.startTime || ''))
  } else {
    const from = day.items.indexOf(item), to = body.targetIndex
    if (to == null || to === from || to >= day.items.length) throw new Error('请选择另一处有效位置')
    const [low, high] = [Math.min(from, to), Math.max(from, to)]
    const affected = day.items.slice(low, high + 1)
    if (affected.some(p => trip.fixedArrangements?.some(f => f.dayNo === day.dayNo && f.name === p.poiName && f.itemType === p.itemType))) throw new Error('这段顺序含固定安排，请先解除固定')
    if (affected.some(p => !p.startTime || !p.endTime || p.endTime <= p.startTime)) throw new Error('请先补齐有效时段')
    const minutes = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5))
    const format = (n: number) => `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`
    const durations = new Map(affected.map(p => [p, minutes(p.endTime!) - minutes(p.startTime!)]))
    const gaps = affected.slice(1).map((p, i) => Math.max(0, minutes(p.startTime!) - minutes(affected[i].endTime!)))
    let cursor = minutes(affected[0].startTime!)
    day.items.splice(to, 0, ...day.items.splice(from, 1))
    day.items.slice(low, high + 1).forEach((p, i) => {
      const finish = cursor + durations.get(p)!
      if (finish >= 1440) throw new Error('排序后跨越午夜，请编辑时间')
      p.startTime = format(cursor); p.endTime = format(finish); cursor = finish + (gaps[i] || 0)
    })
  }
  for (const d of [day, target]) { delete d.miniRoute; d.note = d.items.map(p => `${p.startTime}–${p.endTime} ${p.poiName}`).join('；') }
}
