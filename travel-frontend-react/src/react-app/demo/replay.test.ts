import { describe, expect, it } from 'vitest'
import { baseline, edited, initialState, phases, project, replayReducer } from './replay'

describe('fixed replay', () => {
  it('delivers days in order and caps completion', () => {
    let state = replayReducer(initialState, 'play')
    expect(project(state).dayList).toHaveLength(0)
    for (let i = 0; i < 3; i++) state = replayReducer(state, 'next')
    expect(project(state).dayList.map((day) => day.dayNo)).toEqual([1])
    state = replayReducer(state, 'next')
    expect(project(state).dayList).toHaveLength(2)
    for (let i = 0; i < 10; i++) state = replayReducer(state, 'next')
    expect(state.step).toBe(phases.length - 1)
    expect(state.running).toBe(false)
    expect(project(state)).toBe(baseline)
  })
  it('pauses, resets and prevents premature edits', () => {
    const playing = replayReducer(initialState, 'play')
    expect(replayReducer(playing, 'pause').running).toBe(false)
    expect(replayReducer(playing, 'draft')).toBe(playing)
    expect(replayReducer(playing, 'apply')).toBe(playing)
    expect(replayReducer(playing, 'reset')).toEqual(initialState)
  })
  it('changes only the second day after confirmation and recalculates budget', () => {
    const ready = { ...initialState, step: phases.length - 1 }
    expect(project(replayReducer(ready, 'apply'))).toBe(baseline)
    const draft = replayReducer(ready, 'draft')
    expect(project(draft)).toBe(baseline)
    const applied = replayReducer(draft, 'apply')
    expect(project(applied)).toBe(edited)
    expect(edited.dayList[0]).toEqual(baseline.dayList[0])
    expect(edited.dayList[1].items.some((point) => point.poiName === '杜甫草堂')).toBe(false)
    expect(baseline.totalAmount - edited.totalAmount).toBe(50)
    expect(replayReducer(applied, 'draft')).toEqual(applied)
  })
  it('keeps example facts explicitly unverified and hotel nights correct', () => {
    for (const fixture of [baseline, edited]) {
      expect(fixture.dayList.map((day) => day.items.filter((point) => point.itemType === 'hotel').length)).toEqual([1, 0])
      expect(fixture.dayList.flatMap((day) => day.items).every((point) => point.valueKind === 'estimated' && point.verificationStatus === 'unverified')).toBe(true)
      expect(fixture.totalAmount).toBe(fixture.budgetList.reduce((sum, row) => sum + row.amount, 0))
    }
  })
})
