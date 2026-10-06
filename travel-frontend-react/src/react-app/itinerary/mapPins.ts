/** 地图点位的纯派生逻辑（React 详情页地图卡）：建点、估算计数、外链构造。
 * 深链一律先过 shared/map-link 的白名单再进 href——前端只构造两类查询 URL，
 * 与后端 map_link.py 的出口域同一集合。 */
import { safeMapLink } from '../../shared/map-link'
import type { DayPlan } from '../../types/itinerary'

export interface MapPin {
  /** 组件内点位标识：dayNo:items 序号（TripItem 契约无 id 字段） */
  key: string
  dayNo: number
  /** 当天有序点位序号（从 1 开始）：让用户直观识别当天路线流动 */
  orderInDay: number
  poiName: string
  latitude: number
  longitude: number
  /** valueKind=estimated：位置是估算的，pin 要带徽章（L6 精神：地图上看得出"这个位置是估的"） */
  estimated: boolean
  startTime: string | null
}

const DAY_PIN_COLORS = ['#2563eb', '#059669', '#d97706', '#dc2626', '#7c3aed', '#0d9488', '#db2777']

export function dayPinColor(dayNo: number): string {
  return DAY_PIN_COLORS[(dayNo - 1) % DAY_PIN_COLORS.length]
}

/** 坐标有效性谓词（与后端 map_link._parse_coords 同语义，parity R2 已补齐）：
 * 有限值 + 经纬度范围 + 非 0/0 哨兵；无效坐标视同无坐标——不进 pin、不出深链，
 * 杜绝把 (0,0) 打到几内亚湾、把越界值喂给地图/高德。 */
export function hasValidCoords(
  latitude: number | null | undefined,
  longitude: number | null | undefined,
): boolean {
  if (latitude == null || longitude == null) return false
  const lat = Number(latitude)
  const lon = Number(longitude)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return false
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return false
  return !(lat === 0 && lon === 0)
}

export function buildPins(days: DayPlan[]): MapPin[] {
  const pins: MapPin[] = []
  for (const day of days) {
    let order = 1
    day.items.forEach((item, index) => {
      const lat = item.latitude
      const lon = item.longitude
      if (lat == null || lon == null || !hasValidCoords(lat, lon)) return
      pins.push({
        key: `${day.dayNo}:${index}`,
        dayNo: day.dayNo,
        orderInDay: order++,
        poiName: item.poiName || '未命名地点',
        latitude: lat,
        longitude: lon,
        estimated: item.valueKind === 'estimated',
        startTime: item.startTime ?? null,
      })
    })
  }
  return pins
}

/** 无/无效坐标被隐藏的点位数（地图画不出来，脚注要如实交代；无效视同无坐标）。 */
export function missingCoordCount(days: DayPlan[]): number {
  return days.reduce(
    (sum, day) => sum + day.items.filter((item) => !hasValidCoords(item.latitude, item.longitude)).length,
    0,
  )
}

/** 估算点位数（有坐标的范围内计，与图上徽章口径一致）。 */
export function estimatedPinCount(pins: MapPin[]): number {
  return pins.filter((pin) => pin.estimated).length
}

/** 谷歌 Maps 查询深链（构造后仍过白名单出口，防手滑拼出域外 URL）。 */
export function googleMapsLink(pin: Pick<MapPin, 'latitude' | 'longitude' | 'poiName'>): string {
  return safeMapLink(
    `https://www.google.com/maps/search/?api=1&query=${pin.latitude},${pin.longitude}`,
  )
}

/** 高德标记深链（国内核实主路径）。 */
export function amapLink(pin: Pick<MapPin, 'latitude' | 'longitude' | 'poiName'>): string {
  return safeMapLink(
    `https://uri.amap.com/marker?position=${pin.longitude},${pin.latitude}&name=${encodeURIComponent(pin.poiName)}`,
  )
}
