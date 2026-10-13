// @vitest-environment happy-dom
import { beforeEach, expect, it } from 'vitest'
import { activateIntakeOwner, clearLegacyIntakeStorage, intakeEpoch, intakeHistoryStorage, intakeOwnerIsCurrent, intakeSessionStorage, suspendIntakeOwner } from './intakeStorage'

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); suspendIntakeOwner() })

it('does not read drafts before identity verification and isolates accounts', () => {
  intakeHistoryStorage.setItem('history', 'blocked')
  expect(intakeHistoryStorage.getItem('history')).toBeNull()
  activateIntakeOwner(1)
  intakeHistoryStorage.setItem('history', 'A')
  const previous = intakeEpoch()
  activateIntakeOwner(2)
  expect(intakeHistoryStorage.getItem('history')).toBeNull()
  expect(intakeOwnerIsCurrent(previous)).toBe(false)
  intakeHistoryStorage.setItem('history', 'B')
  activateIntakeOwner(1)
  expect(intakeHistoryStorage.getItem('history')).toBe('A')
})

it('transfers only the current anonymous conversation on first login', () => {
  activateIntakeOwner(null)
  intakeSessionStorage.setItem('current', 'conversation')
  intakeSessionStorage.setItem('pending', 'last message')
  intakeHistoryStorage.setItem('history', 'anonymous history')
  expect(localStorage.length).toBe(0)
  activateIntakeOwner(1, true)
  expect(intakeSessionStorage.getItem('current')).toBe('conversation')
  expect(intakeSessionStorage.getItem('pending')).toBe('last message')
  expect(intakeHistoryStorage.getItem('history')).toBeNull()
  suspendIntakeOwner()
  activateIntakeOwner(1)
  expect(intakeSessionStorage.getItem('current')).toBeNull()
})

it('cleans unowned legacy records without deleting unrelated storage', () => {
  for (const storage of [localStorage, sessionStorage]) {
    storage.setItem('sinan-intake-sessions-v1', 'old private messages')
    storage.setItem('sinan-intake-generation', 'previous run')
    storage.setItem('theme', 'dark')
  }
  clearLegacyIntakeStorage()
  for (const storage of [localStorage, sessionStorage]) {
    expect(storage.getItem('sinan-intake-sessions-v1')).toBeNull()
    expect(storage.getItem('sinan-intake-generation')).toBeNull()
    expect(storage.getItem('theme')).toBe('dark')
  }
})
