/** Synthetic, typed UI example. No recorded model run, network, clock or persistence. */
import type { ItineraryChatMessage } from '../../types/chat'
import type { DayPlan, ItineraryDetail, TripItem } from '../../types/itinerary'

function item(itemType: string, poiName: string, startTime: string, endTime: string, cost: number, latitude: number, longitude: number): TripItem {
  return { itemType, poiName, startTime, endTime, cost, latitude, longitude,
    valueKind: 'estimated', verificationStatus: 'unverified', freshnessStatus: 'unknown',
    reviewRequirement: 'before_departure', remark: '固定示例：费用、位置与开放状态请出发前核实。' }
}

const days: DayPlan[] = [
  { dayId: 1, dayNo: 1, generationStatus: 'SUCCEEDED', theme: '公园、茶馆与成都日常', items: [
    item('attraction', '人民公园', '09:30', '11:30', 0, 30.657, 104.066),
    item('food', '午餐（示例川菜馆）', '12:00', '13:00', 80, 30.658, 104.065),
    item('hotel', '成都住宿（示例，非真实酒店）', '20:00', '21:00', 360, 30.66, 104.06),
  ] },
  { dayId: 2, dayNo: 2, generationStatus: 'SUCCEEDED', theme: '街巷与庭院慢游', items: [
    item('attraction', '宽窄巷子', '10:00', '12:00', 0, 30.662, 104.056),
    item('attraction', '杜甫草堂', '14:00', '16:00', 50, 30.66, 104.028),
    item('food', '晚餐（示例川菜馆）', '17:00', '18:00', 80, 30.662, 104.055),
  ] },
]
const categories: Record<string, string> = { attraction: '门票', food: '餐饮', hotel: '住宿' }

function withBudget(dayList: DayPlan[]): ItineraryDetail {
  const amounts = new Map<string, { category: string; amount: number; itemCount: number }>()
  for (const day of dayList) for (const point of day.items) {
    const category = categories[point.itemType] || '其他'
    const row = amounts.get(category) || { category, amount: 0, itemCount: 0 }
    row.amount += point.cost || 0
    row.itemCount += 1
    amounts.set(category, row)
  }
  const budgetList = [...amounts.values()]
  return { id: 0, title: '成都 · 两天一晚固定示例', city: '成都', days: 2, stayNights: 1,
    persons: 2, budget: 1000, status: 2, genState: 'COMPLETED', qualityStatus: 'READY_WITH_WARNINGS',
    destinationStatus: 'draft_only', pendingFactCount: dayList.reduce((n, day) => n + day.items.length, 0),
    planNote: '合成固定示例，不是实际模型运行结果；不用于评价模型质量。',
    dayList, budgetList, totalAmount: budgetList.reduce((n, row) => n + row.amount, 0), sources: [], suggestions: [] }
}

export const baseline = withBudget(days)
const relaxedDay = { ...days[1], theme: '少赶一站，把下午留白', items: days[1].items.filter((point) => point.poiName !== '杜甫草堂') }
export const edited = withBudget([days[0], relaxedDay])
export const editMessage: ItineraryChatMessage = {
  role: 'ai', content: '固定编辑示例：移除第二天的杜甫草堂，保留第一天与住宿安排。确认后生效。',
  plans: [{ day_no: 2, items: relaxedDay.items.map((point) => ({ poi_name: point.poiName, item_type: point.itemType,
    start_time: point.startTime, end_time: point.endTime, cost: point.cost, latitude: point.latitude, longitude: point.longitude })) }],
}

export const phases = ['旅行需求', '解析需求', '检索与规划', '交付第一天', '交付第二天', '检查与格式化', '行程就绪'] as const
export interface ReplayState { step: number; running: boolean; showDraft: boolean; applied: boolean }
export const initialState: ReplayState = { step: 0, running: false, showDraft: false, applied: false }
export type ReplayAction = 'play' | 'pause' | 'next' | 'reset' | 'draft' | 'apply'
export function replayReducer(state: ReplayState, action: ReplayAction): ReplayState {
  if (action === 'reset') return { ...initialState }
  if (action === 'pause') return { ...state, running: false }
  if (action === 'play') return { ...state, running: state.step < phases.length - 1 }
  if (action === 'next') {
    const step = Math.min(state.step + 1, phases.length - 1)
    return { ...state, step, running: state.running && step < phases.length - 1 }
  }
  if (state.step !== phases.length - 1) return state
  if (action === 'draft' && !state.applied) return { ...state, showDraft: true }
  if (action === 'apply' && state.showDraft) return { ...state, showDraft: false, applied: true }
  return state
}

export function project(state: ReplayState): ItineraryDetail {
  if (state.step === phases.length - 1) return state.applied ? edited : baseline
  const visible = days.filter((day) => state.step >= day.dayNo + 2)
  return { ...withBudget(visible), status: 1, genState: 'GENERATING', qualityStatus: 'DRAFT' }
}
