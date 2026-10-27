import { describe, expect, it } from 'vitest'
import { makeDemoTrip } from './fixtures'
import { createDemoItem, editDemoItem } from './itemAction'

describe('manual demo edits', () => {
  it.each([false, true])('includes newly added costs in the saved demo budget (fixed=%s)', fixed => {
    const original = makeDemoTrip()
    const next = createDemoItem(original, { planningRevision: 1, action: 'create', dayNo: 1, itemType: 'food',
      poiName: '自备早餐', startTime: '08:00', endTime: '08:30', cost: 10, address: null, remark: '用户填写', fixed, latitude: null, longitude: null })
    expect(next.totalAmount).toBe(original.totalAmount + 10 * original.persons)
    expect(next.dayList[0].items.some(p => p.poiName === '自备早餐')).toBe(true)
    expect(next.fixedArrangements?.some(p => p.name === '自备早餐') || false).toBe(fixed)
    expect(original.dayList[0].items.some(p => p.poiName === '自备早餐')).toBe(false)
  })
  const base = { poiName: null, address: null, cost: null, remark: null, planningRevision: 1, dayNo: 1, startTime: '09:30', endTime: '10:00', targetIndex: null }
  it('edits only the selected item and rejects overlapping times without changing the source', () => {
    const trip = makeDemoTrip()
    const edited = editDemoItem(trip, 11, { ...base, action: 'update' })
    expect(edited.dayList[0].items[0].endTime).toBe('10:00')
    expect(edited.dayList[1]).toEqual(trip.dayList[1])
    expect(() => editDemoItem(trip, 11, { ...base, action: 'update', endTime: '11:00' })).toThrow('重叠')
    expect(trip.dayList[0].items[0].endTime).toBe('10:30')
  })
  it('persists fixed arrangements in the returned snapshot and requires explicit unfix', () => {
    const fixed = editDemoItem(makeDemoTrip(), 11, { ...base, action: 'fix' })
    expect(fixed.fixedArrangements?.[0].name).toBe('人民公园')
    expect(() => editDemoItem(fixed, 11, { ...base, action: 'delete' })).toThrow('解除固定')
    const unlocked = editDemoItem(fixed, 11, { ...base, action: 'unfix' })
    expect(editDemoItem(unlocked, 11, { ...base, action: 'delete' }).dayList[0].items.some(p => p.id === 11)).toBe(false)
  })
})
