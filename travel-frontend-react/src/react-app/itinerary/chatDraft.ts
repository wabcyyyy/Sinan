/** 对话草稿的纯派生逻辑（CH3）：diff 摘要、未核实点位、活动草稿定位、酒店默认选中。
 * 从已退役的旧版编辑面板 ChatEditPanel 语义移植（draftChanges/prepareHotelOptions），供现壳与单测共用。 */
import type { ChatDayPlan, ChatPlanItem, ItineraryChatMessage } from '../../types/chat'
import type { DayPlan, HotelOption, TripItem } from '../../types/itinerary'

/** 最近一条带草稿（plans 或 hotelOptions）的 AI 消息 =「当前唯一待确认」。 */
export function activeActionIndex(msgs: ItineraryChatMessage[]): number {
  for (let i = msgs.length - 1; i >= 0; i--) {
    const msg = msgs[i]
    if (msg.role !== 'ai') continue
    if ((msg.plans?.length ?? 0) > 0 || (msg.hotelOptions?.length ?? 0) > 0) return i
  }
  return -1
}

const tripNames = (day?: DayPlan) =>
  (day?.items || []).map((item) => item.poiName?.trim()).filter((name): name is string => Boolean(name))

const draftNames = (day: ChatDayPlan) =>
  (day.items || []).map((item) => item.poi_name?.trim()).filter((name): name is string => Boolean(name))

/** 草稿 vs 现行程的逐天差异，供草稿卡摘要；看不出差异时给兜底文案。 */
export function draftChanges(current: DayPlan[], draft: ChatDayPlan[]): string[] {
  const out: string[] = []
  for (const day of draft) {
    const oldSet = new Set(tripNames(current.find((item) => item.dayNo === day.day_no)))
    const added = draftNames(day).filter((name) => !oldSet.has(name))
    if (added.length) out.push(`第 ${day.day_no} 天新增：${added.join('、')}`)
  }
  for (const day of draft) {
    const newSet = new Set(draftNames(day))
    const removed = tripNames(current.find((item) => item.dayNo === day.day_no)).filter((name) => !newSet.has(name))
    if (removed.length) out.push(`第 ${day.day_no} 天删除：${removed.join('、')}`)
  }
  return out.length ? out : ['计划内容已更新，请核对下方完整安排']
}

export interface StructuredChange {
  /** 五类差异：新增 / 移除 / 时间或费用变化 / 跨日移动 / 酒店身份变化（spec M4 §9.1） */
  type: 'added' | 'removed' | 'updated' | 'moved' | 'hotelChanged'
  dayNo?: number
  label: string
  items: string[]
  /** updated：变化维度（time=起止/时长，cost=估算费用）；兜底条目缺省=不出徽标 */
  field?: 'time' | 'cost'
  /** updated：时间窗或费用的前后值（费用渲染时加 ¥） */
  from?: string
  to?: string
  /** moved：原天 → 新天 */
  fromDay?: number
  toDay?: number
  /** hotelChanged：原住宿名 → 新住宿名 */
  fromName?: string
  toName?: string
}

const cleanName = (name?: string | null) => name?.trim() ?? ''

const isHotelDraft = (item: ChatPlanItem) => item.item_type === 'hotel'
const isHotelTrip = (item: TripItem) => item.itemType === 'hotel'

/** 时间窗展示：两端齐才拼 start-end，只有一端给该端，全缺给空串（= 无法比较，不报变化）。 */
const timeWindow = (start?: string | null, end?: string | null) => {
  const s = start?.trim() ?? ''
  const e = end?.trim() ?? ''
  return s && e ? `${s}-${e}` : s || e
}

/** 同名同天的逐字段比较：时间窗优先，其次时长；费用独立一条。两侧都有值且不等才报（缺省≠变化）。 */
function updatedChanges(dayNo: number, name: string, base: TripItem, item: ChatPlanItem): StructuredChange[] {
  const out: StructuredChange[] = []
  const fromWin = timeWindow(base.startTime, base.endTime)
  const toWin = timeWindow(item.start_time, item.end_time)
  if (fromWin && toWin && fromWin !== toWin) {
    out.push({ type: 'updated', dayNo, field: 'time', label: `第 ${dayNo} 天`, items: [name], from: fromWin, to: toWin })
  } else if (base.durationMin != null && item.duration_min != null && base.durationMin !== item.duration_min) {
    out.push({
      type: 'updated', dayNo, field: 'time', label: `第 ${dayNo} 天`, items: [name],
      from: `${base.durationMin} 分钟`, to: `${item.duration_min} 分钟`,
    })
  }
  if (base.cost != null && item.cost != null && base.cost !== item.cost) {
    out.push({ type: 'updated', dayNo, field: 'cost', label: `第 ${dayNo} 天`, items: [name], from: String(base.cost), to: String(item.cost) })
  }
  return out
}

/** 结构化草稿差异（五类，spec M4 §9.1）：新增/移除保持按天分组的既有语义；
 * 同名同天比时间与费用，同名换天报 moved，酒店条目单独比身份（不进逐日增删）。
 * 看不出差异时给兜底文案（type=updated 且无 from/to，渲染为中性行）。 */
export function structuredDraftChanges(current: DayPlan[], draft: ChatDayPlan[]): StructuredChange[] {
  // 名称归一后的逐日点位表（酒店剔除，走 hotelChanged）
  const baseByDay = new Map<number, Map<string, TripItem>>()
  for (const day of current) {
    const map = new Map<string, TripItem>()
    for (const item of day.items || []) {
      const name = cleanName(item.poiName)
      if (name && !isHotelTrip(item)) map.set(name, item)
    }
    baseByDay.set(day.dayNo, map)
  }
  const draftByDay = new Map<number, Map<string, ChatPlanItem>>()
  for (const day of draft) {
    const map = new Map<string, ChatPlanItem>()
    for (const item of day.items || []) {
      const name = cleanName(item.poi_name)
      if (name && !isHotelDraft(item)) map.set(name, item)
    }
    draftByDay.set(day.day_no, map)
  }

  // 跨日移动先算：基线第 X 天有、草稿第 X 天没有、但草稿别的天有 → moved（从增删里剔除）
  const moves: StructuredChange[] = []
  const movedInto = new Set<string>() // `名称@目标天`
  for (const [fromDay, baseMap] of baseByDay) {
    for (const name of baseMap.keys()) {
      if (draftByDay.get(fromDay)?.has(name)) continue
      const toDay = [...draftByDay.entries()].find(([d, dm]) => d !== fromDay && dm.has(name))?.[0]
      if (toDay == null) continue
      movedInto.add(`${name}@${toDay}`)
      moves.push({ type: 'moved', dayNo: toDay, label: name, items: [name], fromDay, toDay })
    }
  }

  const out: StructuredChange[] = []
  for (const day of draft) {
    const baseMap = baseByDay.get(day.day_no)
    const added = [...(draftByDay.get(day.day_no)?.keys() ?? [])].filter((name) => !baseMap?.has(name) && !movedInto.has(`${name}@${day.day_no}`))
    if (added.length) out.push({ type: 'added', dayNo: day.day_no, label: `第 ${day.day_no} 天新增`, items: added })
  }
  for (const day of draft) {
    const baseMap = baseByDay.get(day.day_no)
    const dayMap = draftByDay.get(day.day_no)
    const removed = [...(baseMap?.keys() ?? [])].filter((name) => !dayMap?.has(name) && !moves.some((m) => m.items[0] === name && m.fromDay === day.day_no))
    if (removed.length) out.push({ type: 'removed', dayNo: day.day_no, label: `第 ${day.day_no} 天移除`, items: removed })
  }
  for (const day of draft) {
    const baseMap = baseByDay.get(day.day_no)
    if (!baseMap) continue
    for (const [name, item] of draftByDay.get(day.day_no) ?? []) {
      const base = baseMap.get(name)
      if (base) out.push(...updatedChanges(day.day_no, name, base, item))
    }
  }
  out.push(...moves)
  const hotelNames = (names: string[]) => names.filter(Boolean).join('、')
  const fromHotel = hotelNames(current.flatMap((day) => (day.items || []).filter(isHotelTrip).map((item) => cleanName(item.poiName))))
  const toHotel = hotelNames(draft.flatMap((day) => (day.items || []).filter(isHotelDraft).map((item) => cleanName(item.poi_name))))
  if (fromHotel && toHotel && fromHotel !== toHotel) {
    out.push({ type: 'hotelChanged', label: '住宿调整', items: [], fromName: fromHotel, toName: toHotel })
  }
  if (!out.length) {
    out.push({
      type: 'updated',
      label: '计划内容已更新',
      items: ['请核对下方完整安排'],
    })
  }
  return out
}

/** 差异条目的一句话文案（草稿卡 diff 行）；措辞与 draftChanges 的「新增/移除」口径一致。 */
export function describeStructuredChange(change: StructuredChange): string {
  switch (change.type) {
    case 'added':
    case 'removed':
      return `${change.label}：${change.items.join('、')}`
    case 'updated': {
      if (change.from == null || change.to == null) return `${change.label}：${change.items.join('、')}`
      const name = change.items[0] ?? change.label
      const day = change.dayNo != null ? `（第 ${change.dayNo} 天）` : ''
      const value = change.field === 'cost' ? `¥${change.from}→¥${change.to}` : `${change.from}→${change.to}`
      return `${name}${day}：${value}`
    }
    case 'moved':
      return `${change.label}：第 ${change.fromDay} 天→第 ${change.toDay} 天`
    case 'hotelChanged':
      return `住宿调整：${change.fromName}→${change.toName}`
  }
}

/** 差异徽标（五类）：+新增 / −移除 / ~时间|~费用 / →跨日 / 酒店；无字段信息的中性兜底条目不出徽标。 */
export function diffBadge(change: StructuredChange): { text: string; className: string } | null {
  switch (change.type) {
    case 'added':
      return { text: '+新增', className: 'is-add' }
    case 'removed':
      return { text: '−移除', className: 'is-remove' }
    case 'updated':
      if (change.field === 'cost') return { text: '~费用', className: 'is-update' }
      return change.from != null || change.to != null ? { text: '~时间', className: 'is-update' } : null
    case 'moved':
      return { text: '→跨日', className: 'is-move' }
    case 'hotelChanged':
      return { text: '酒店', className: 'is-hotel' }
  }
}

/** AI 新增且没有坐标的点位：位置未经核实，应用前提醒一句（证据体系红线）。 */
export function unverifiedNames(current: DayPlan[], draft: ChatDayPlan[]): string[] {
  const oldSet = new Set(current.flatMap((day) => tripNames(day)))
  const fresh = (items: ChatPlanItem[] | undefined) =>
    (items || []).filter(
      (item) =>
        item.poi_name?.trim() &&
        !oldSet.has(item.poi_name.trim()) &&
        (item.latitude == null || item.longitude == null),
    )
  return draft.flatMap((day) => fresh(day.items).map((item) => `${item.poi_name?.trim()}（第 ${day.day_no} 天）`))
}

export interface HotelSelection {
  roomType: string
  dayNos: number[]
}

/** 酒店候选的默认选中：默认房型（isDefault 优先，否则第一个）+ 后端给的入住晚次。 */
export function hotelDefaultSelection(option: HotelOption, tripDays: number): HotelSelection {
  const rooms = option.roomTypes || []
  const room = rooms.find((item) => item.isDefault) || rooms[0]
  const nights = option.requestedDayNos?.length
    ? option.requestedDayNos
    : Array.from({ length: Math.max(Math.min(option.nights || 1, tripDays), 1) }, (_, index) => index + 1)
  return { roomType: room?.roomName || '', dayNos: nights }
}

/** 确认卡的提案一句话（L2：确认卡长在对话流里）。 */
export function pendingActionSummary(pending: NonNullable<ItineraryChatMessage['pendingAction']>): string {
  const hotels = pending.hotel_names?.length ? pending.hotel_names.join('、') : '当前住宿'
  const nights = pending.night_count ? `，共 ${pending.night_count} 晚` : ''
  return `AI 提议更换住宿：${hotels}${pending.target_tier ? `（${pending.target_tier}）` : ''}${nights}`
}
