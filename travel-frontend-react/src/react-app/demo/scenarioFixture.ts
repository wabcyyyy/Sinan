/** 固定成都返程案例，非第二套排程算法。条件不匹配时由 transport 拒绝。 */
import type { ScenarioCompareVO } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import type { ChatDayPlan } from '../../types/chat'

export function scenarioFixture(trip: ItineraryDetail, revision: number): { comparison: ScenarioCompareVO; plans: ChatDayPlan[] } {
  const points = trip.dayList[1].items
  const times = [['09:30', '11:30'], ['12:00', '13:00'], ['13:30', '15:00'], ['15:30', '16:30'], ['17:00', '18:00']]
  const items = points.map((point, index) => ({ id: point.id!, name: point.poiName, startTime: times[index][0], endTime: times[index][1], fixed: index === 0 }))
  return {
    comparison: { planningRevision: revision, assumptions: ['固定样例：每次转场 30 分钟，16:00 返程另留 30 分钟；这些是演示假设，不是交通测算。', '保留杜甫草堂原时段、第一天与住宿。固定选择仅在这次调整生效。'], options: [
      { key: 'keep', title: '保留全部安排', feasible: false, conflicts: ['最晚 18:00 结束，比可用截止 15:30 晚 150 分钟'], items, removed: [], finishTime: '18:00', slackMinutes: -150, costDelta: 0 },
      { key: 'omit', title: '移除可选安排', feasible: true, conflicts: [], items: items.slice(0, 3), removed: points.slice(3).map(point => point.poiName), finishTime: '15:00', slackMinutes: 30, costDelta: -230 },
    ] },
    plans: [{ day_no: 2, items: points.slice(0, 3).map((point, index) => ({ poi_name: point.poiName, item_type: point.itemType, start_time: times[index][0], end_time: times[index][1], cost: point.cost, latitude: point.latitude, longitude: point.longitude })) }],
  }
}
