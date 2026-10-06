/** 对话草稿的纯派生逻辑（CH3）：diff 摘要、未核实点位、活动草稿定位、酒店默认选中。
 * 从已退役的旧版编辑面板 ChatEditPanel 语义移植（draftChanges/prepareHotelOptions），供现壳与单测共用。 */
import type { ChatDayPlan, ChatPlanItem, ItineraryChatMessage } from '../../types/chat'
import type { DayPlan, HotelOption } from '../../types/itinerary'

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
  type: 'add' | 'remove' | 'update'
  dayNo?: number
  label: string
  items: string[]
}

/** 结构化草稿差异：供草稿卡渲染清晰的状态徽标与地点胶囊 */
export function structuredDraftChanges(current: DayPlan[], draft: ChatDayPlan[]): StructuredChange[] {
  const out: StructuredChange[] = []
  for (const day of draft) {
    const oldSet = new Set(tripNames(current.find((item) => item.dayNo === day.day_no)))
    const added = draftNames(day).filter((name) => !oldSet.has(name))
    if (added.length) {
      out.push({
        type: 'add',
        dayNo: day.day_no,
        label: `第 ${day.day_no} 天新增`,
        items: added,
      })
    }
  }
  for (const day of draft) {
    const newSet = new Set(draftNames(day))
    const removed = tripNames(current.find((item) => item.dayNo === day.day_no)).filter((name) => !newSet.has(name))
    if (removed.length) {
      out.push({
        type: 'remove',
        dayNo: day.day_no,
        label: `第 ${day.day_no} 天移除`,
        items: removed,
      })
    }
  }
  if (!out.length) {
    out.push({
      type: 'update',
      label: '计划内容已更新',
      items: ['请核对下方完整安排'],
    })
  }
  return out
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
