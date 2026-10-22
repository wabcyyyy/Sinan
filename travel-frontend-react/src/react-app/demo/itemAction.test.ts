import { describe, expect, it } from 'vitest'
import { makeDemoTrip } from './fixtures'
import { editDemoItem } from './itemAction'

describe('manual demo edits', () => {
  const base = { poiName: null, address: null, cost: null, remark: null, planningRevision: 1, dayNo: 1, startTime: '09:30', endTime: '10:00' }
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
