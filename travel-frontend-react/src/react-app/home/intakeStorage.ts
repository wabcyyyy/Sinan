import { isDemoMode, storageKey } from '../../demoMode'

/** Draft ownership comes only from the server-confirmed identity. */
let owner: string | null = null
let epoch = 0
const prefix = 'sinan-intake-v2:'
const legacyKeys = ['sinan-intake-v1', 'sinan-intake-sessions-v1', 'sinan-intake-pending', 'sinan-intake-generation', 'sinan-intake-attempt']

export function clearLegacyIntakeStorage(): void {
  for (const storage of [localStorage, sessionStorage]) {
    for (const key of legacyKeys) storage.removeItem(key)
  }
}

export function intakeEpoch(): number { return epoch }
export function intakeOwnerIsCurrent(value: number): boolean { return value === epoch && owner !== null }
export function hasIntakeUser(): boolean { return owner !== null && owner !== 'anonymous' }

export function activateIntakeOwner(userId: number | null, transferAnonymous = false): void {
  const next = userId === null ? 'anonymous' : `user-${userId}`
  if (next === owner) return
  const transfer = transferAnonymous && owner === 'anonymous'
    ? ['current', 'pending'].map((key) => [key, intakeSessionStorage.getItem(key)] as const) : []
  if (owner) {
    try {
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const key = sessionStorage.key(i)
      if (key?.startsWith(storageKey(`${prefix}${owner}:`))) sessionStorage.removeItem(key)
    }
    } catch { /* unavailable storage */ }
  }
  owner = next
  epoch += 1
  for (const [key, value] of transfer) {
    try { if (value !== null) intakeSessionStorage.setItem(key, value) } catch { /* unavailable storage */ }
  }
}

export function suspendIntakeOwner(): void {
  if (owner) {
    try {
      for (let i = sessionStorage.length - 1; i >= 0; i--) {
        const key = sessionStorage.key(i)
        if (key?.startsWith(storageKey(`${prefix}${owner}:`))) sessionStorage.removeItem(key)
      }
    } catch { /* unavailable storage */ }
  }
  owner = null
  epoch += 1
}

function scopedStorage(history: boolean) {
  const storage = () => history && hasIntakeUser() && !isDemoMode() ? localStorage : sessionStorage
  const scopedKey = (key: string) => storageKey(`${prefix}${owner}:${key}`)
  return {
    getItem(key: string): string | null {
      try { return owner === null ? null : storage().getItem(scopedKey(key)) } catch { return null }
    },
    setItem(key: string, value: string): void {
      if (owner !== null) storage().setItem(scopedKey(key), value)
    },
    removeItem(key: string): void {
      try { if (owner !== null) storage().removeItem(scopedKey(key)) } catch { /* unavailable storage */ }
    },
  }
}

export const intakeSessionStorage = scopedStorage(false)
export const intakeHistoryStorage = scopedStorage(true)
