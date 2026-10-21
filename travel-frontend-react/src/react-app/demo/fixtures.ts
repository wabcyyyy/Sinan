/** 合成样例，仅供正式页面的静态交互展示；不是模型运行记录。 */
import type { DayPlan, ItineraryDetail, TripItem } from '../../types/itinerary'
import type { ChatDayPlan } from '../../types/chat'
import type { TemplateDetailVO } from '../../types/generated/contracts'

function item(id: number, itemType: string, poiName: string, startTime: string, endTime: string, cost: number, latitude: number, longitude: number, remark: string): TripItem {
  return { id, itemType, poiName, startTime, endTime, cost, latitude, longitude, address: '成都 · 样例位置待核实', remark,
    whyThis: '按街区串联，给用餐与休息留出时间。', intro: remark, valueKind: 'estimated', verificationStatus: 'unverified',
    freshnessStatus: 'unknown', reviewRequirement: 'before_departure' }
}

export function recalculate(trip: ItineraryDetail): ItineraryDetail {
  const categories: Record<string, string> = { attraction: '门票', food: '餐饮', hotel: '住宿', activity: '体验', transport: '交通' }
  const amounts = new Map<string, { category: string; amount: number; itemCount: number }>()
  for (const day of trip.dayList) for (const point of day.items) {
    const category = categories[point.itemType] || '其他'
    const row = amounts.get(category) || { category, amount: 0, itemCount: 0 }
    // 与页面单价单位一致：普通条目按人、住宿按每间两人；不虚构未列出的交通费。
    const units = point.itemType === 'hotel' ? Math.ceil(trip.persons / 2) : trip.persons
    row.amount += (point.cost || 0) * units
    row.itemCount += 1
    amounts.set(category, row)
  }
  const budgetList = [...amounts.values()]
  return { ...trip, budgetList, totalAmount: budgetList.reduce((sum, row) => sum + row.amount, 0),
    pendingFactCount: trip.dayList.reduce((sum, day) => sum + day.items.length, 0) }
}

const dayList: DayPlan[] = [
  { dayId: 1, dayNo: 1, generationStatus: 'SUCCEEDED', theme: '茶馆、街巷与成都日常', note: '从一杯茶开始，慢慢进入成都的节奏。',
    items: [
      item(11, 'attraction', '人民公园', '09:30', '10:30', 0, 30.657, 104.055, '沿湖边和树荫慢走，不必把整个公园走完。'),
      item(12, 'activity', '鹤鸣茶社喝茶（示例）', '10:30', '11:30', 35, 30.656, 104.054, '留一个小时坐下来，观察成都的茶馆日常。茶位与消费须到店核实。'),
      item(13, 'food', '少城午餐（示例川菜馆）', '12:00', '13:00', 80, 30.659, 104.054, '就近用餐，菜单与人均为展示估算。'),
      item(14, 'attraction', '成都博物馆', '14:00', '16:00', 0, 30.655, 104.063, '用一个下午了解城市历史。请提前核实预约、闭馆日与展览安排。'),
      item(15, 'food', '老街晚餐（示例）', '17:30', '18:30', 80, 30.657, 104.06, '晚餐后不再跨区赶路，留一点时间散步。'),
      item(16, 'hotel', '少城住宿（合成示例酒店）', '20:00', '21:00', 300, 30.661, 104.054, '展示住宿确认交互，非可预订酒店，价格不是实时报价。'),
    ], practicalNotes: ['以上地点坐标、费用与开放状态均为估算，出发前请核实。'],
    backupPlan: [{ if: '下雨', action: '缩短户外散步，在茶馆多停留，场馆开放与预约仍须核实。' }],
    photoSpots: [{ name: '人民公园湖边树荫', bestTime: '上午', tip: '把茶杯和街景留在同一张照片里。' }] },
  { dayId: 2, dayNo: 2, generationStatus: 'SUCCEEDED', theme: '庭院、老街与慢调午后', note: '把傍晚留给返程，不安排住宿。',
    items: [
      item(21, 'attraction', '杜甫草堂', '09:30', '11:30', 50, 30.66, 104.028, '慢看庭院与竹林，门票按示例估算，请核实当日价格。'),
      item(22, 'food', '草堂附近午餐（示例）', '12:00', '13:00', 80, 30.661, 104.031, '午餐之后留出交通余量，再去少城街区。'),
      item(23, 'attraction', '宽窄巷子', '14:00', '15:30', 0, 30.663, 104.052, '按体力选一段老街，不以打卡数量为目标。'),
      item(24, 'activity', '街角咖啡与自由散步（示例）', '16:00', '17:00', 35, 30.664, 104.053, '可以坐下来整理照片，也可以回到喜欢的小巷。'),
      item(25, 'food', '返程前晚餐（示例）', '17:30', '18:30', 80, 30.662, 104.054, '按返程时间调整用餐，车站交通时间请另行核实。'),
    ], practicalNotes: ['这是固定样例，门票与营业状态未实时查询；费用小计按两人计算，不含未列出的交通费。'],
    backupPlan: [{ if: '体力不足', action: '在对话栏选择“把第二天安排松一点”，确认后移除上午的杜甫草堂。' }], photoSpots: [],
  },
]

export function makeDemoTrip(id = 101): ItineraryDetail {
  return recalculate(structuredClone({ id, title: '成都 · 茶馆与街巷两日慢游', city: '成都', days: 2, stayNights: 1, persons: 2,
    budget: 1200, preferences: '美食,慢节奏,人文历史', favorite: true, status: 2, genState: 'COMPLETED',
    destinationStatus: 'draft_only', qualityStatus: 'READY_WITH_WARNINGS', tripTheme: '把脚步放慢，把世界看近',
    intent: '两个人去成都两天一晚，喜欢茶馆和老街，预算 1200 元，不想太赶。',
    planNote: '合成展示样例：逐日路线、费用和说明用于体验完整界面，并非真实 AI 输出或实时地点信息。',
    dayList, budgetList: [], totalAmount: 0, sources: [], suggestions: [],
  } as ItineraryDetail))
}

export function relaxedPlans(trip: ItineraryDetail): ChatDayPlan[] {
  const day = trip.dayList[1]
  const remove = day.items.find((point) => point.poiName === '杜甫草堂') || [...day.items].reverse().find((point) => point.itemType === 'attraction')
  return [{ day_no: day.dayNo, items: day.items.filter((point) => point !== remove).map((point) => ({
    poi_name: point.poiName, item_type: point.itemType, start_time: point.startTime, end_time: point.endTime,
    cost: point.cost, latitude: point.latitude, longitude: point.longitude, remark: point.remark,
  })) }]
}

export function makeTemplateTrip(template: TemplateDetailVO, id: number): ItineraryDetail {
  return recalculate({ id, title: `模板:${template.title}`, city: template.city, days: template.days, stayNights: Math.max(template.days - 1, 0), persons: template.summary.persons,
    status: 2, genState: 'COMPLETED', destinationStatus: 'draft_only', qualityStatus: 'READY_WITH_WARNINGS', favorite: false,
    planNote: '从编辑规划复制，日期、住宿与预算需自行设置；本标签页内的修改不影响公开参考路线。',
    dayList: template.summary.dayList.map((day) => ({ dayId: id * 10 + day.dayNo, dayNo: day.dayNo, theme: day.theme, generationStatus: 'SUCCEEDED', items: day.items.map((point, index) => ({
      id: id * 100 + day.dayNo * 10 + index, itemType: point.itemType || 'attraction', poiName: point.poiName, address: point.address,
      startTime: point.startTime, endTime: point.endTime, latitude: point.latitude, longitude: point.longitude, remark: point.tip,
      verificationStatus: 'unverified', valueKind: 'estimated', freshnessStatus: 'unknown', reviewRequirement: 'before_departure',
    })) })), budgetList: [], totalAmount: 0,
  })
}
