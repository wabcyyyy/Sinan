import { describe, expect, it } from 'vitest'
import type { DayPlan } from '../../types/itinerary'
import {
  amapLink,
  buildPins,
  dayPinColor,
  estimatedPinCount,
  googleMapsLink,
  missingCoordCount,
  poiPhotoUrl,
} from './mapPins'

const day = (dayNo: number, items: Array<{ name: string; lat?: number | null; lng?: number | null; kind?: string; time?: string }>): DayPlan =>
  ({
    dayId: dayNo,
    dayNo,
    generationStatus: 'SUCCEEDED',
    items: items.map((item, index) => ({
      itemType: 'attraction',
      poiName: item.name,
      latitude: item.lat ?? null,
      longitude: item.lng ?? null,
      valueKind: item.kind ?? 'observed',
      startTime: item.time ?? null,
      key: index,
    })),
  }) as unknown as DayPlan

describe('buildPins（建点：无坐标跳过，estimated 独立标记）', () => {
  it('无坐标点位不进 pin，estimated 按 valueKind 判定', () => {
    const pins = buildPins([
      day(1, [
        { name: '断桥残雪', lat: 30.26, lng: 120.15, time: '09:00' },
        { name: '估算的博物馆', lat: 30.28, lng: 120.16, kind: 'estimated' },
        { name: '没坐标的茶馆' },
      ]),
    ])
    expect(pins).toHaveLength(2)
    expect(pins[0]).toMatchObject({ dayNo: 1, orderInDay: 1, poiName: '断桥残雪', estimated: false, startTime: '09:00' })
    expect(pins[1]).toMatchObject({ dayNo: 1, orderInDay: 2 })
    expect(pins[0].key).toBe('1:0')
    expect(pins[1].estimated).toBe(true)
  })
  it('generated 也视为估算徽章之外的原样 kind', () => {
    const pins = buildPins([day(2, [{ name: 'x', lat: 1, lng: 1, kind: 'generated' }])])
    expect(pins[0].estimated).toBe(false)
  })
})

describe('hasValidCoords（无效坐标视同无坐标，parity R2 已补齐）', () => {
  it('0/0 哨兵与越界坐标不进 pin、计入隐藏数', () => {
    const pins = buildPins([
      day(1, [
        { name: '哨兵点', lat: 0, lng: 0 },
        { name: '越界点', lat: 200, lng: -95 },
        { name: '正常点', lat: 30.26, lng: 120.15 },
      ]),
    ])
    expect(pins.map((pin) => pin.poiName)).toEqual(['正常点'])
    expect(missingCoordCount([day(1, [
      { name: '哨兵点', lat: 0, lng: 0 },
      { name: '越界点', lat: 200, lng: -95 },
    ])])).toBe(2)
  })
  it('边界值合法：±90/±180 有效，±90 之外无效', () => {
    expect(buildPins([day(1, [{ name: '极点', lat: 90, lng: 180 }])])).toHaveLength(1)
    expect(buildPins([day(1, [{ name: '出界', lat: 90.01, lng: 0 }])])).toHaveLength(0)
  })
})

describe('计数（脚注口径）', () => {
  const days = [
    day(1, [{ name: 'a', lat: 1, lng: 1, kind: 'estimated' }, { name: 'b' }]),
    day(2, [{ name: 'c', lat: 2, lng: 2 }]),
  ]
  it('估算计数只数有坐标的 pin', () => {
    const pins = buildPins(days)
    expect(estimatedPinCount(pins)).toBe(1)
    expect(missingCoordCount(days)).toBe(1)
  })
  it('pin 颜色按天循环取色', () => {
    expect(dayPinColor(1)).toBe(dayPinColor(8))
    expect(dayPinColor(1)).not.toBe(dayPinColor(2))
  })
})

describe('深链构造（构造后必须能过 safeMapLink 白名单）', () => {
  const pin = { latitude: 30.26, longitude: 120.15, poiName: '断桥残雪' }
  it('谷歌链是 /maps 路径查询', () => {
    const link = googleMapsLink(pin)
    expect(link.startsWith('https://www.google.com/maps/search/?api=1&query=30.26,120.15')).toBe(true)
  })
  it('高德链是 marker URI，name 经编码', () => {
    const link = amapLink(pin)
    expect(link.startsWith('https://uri.amap.com/marker?position=120.15,30.26&name=')).toBe(true)
    expect(link).toContain(encodeURIComponent('断桥残雪'))
  })
})

describe('poiPhotoUrl（点位实景链，替代跨城市封面兜底）', () => {
  it('同源 poi-photo 路径，name/city 经编码', () => {
    const url = poiPhotoUrl('断桥残雪', '杭州')
    expect(url?.startsWith('/api/poi-photo?')).toBe(true)
    expect(url).toContain(encodeURIComponent('断桥残雪'))
    expect(url).toContain(encodeURIComponent('杭州'))
  })
  it('名字为空或超长（服务端 64 字上限）返回 undefined，调用方不渲染图', () => {
    expect(poiPhotoUrl('', '杭州')).toBeUndefined()
    expect(poiPhotoUrl('   ', '杭州')).toBeUndefined()
    expect(poiPhotoUrl('名'.repeat(65), '杭州')).toBeUndefined()
  })
})
