import type { IntakeMessage, IntakeSlots } from './intakeSlots'

export const INTAKE_SESSIONS_STORAGE_KEY = 'sinan-intake-sessions-v1'

export interface IntakeSessionRecord {
  id: string
  title: string
  updatedAt: number
  slots: IntakeSlots
  messages: IntakeMessage[]
  firstMessage: string
  generationId?: string | null
}

export function deriveSessionTitle(slots: IntakeSlots, firstMessage: string): string {
  if (slots.city) {
    const parts = [slots.city]
    if (slots.days) parts.push(`${slots.days}天`)
    if (slots.persons) parts.push(`${slots.persons}人`)
    return `${parts.join(' · ')}规划`
  }
  if (firstMessage.trim()) {
    const clean = firstMessage.trim().slice(0, 30)
    return clean.length >= 30 ? `${clean}…` : clean
  }
  return '未命名旅行想法'
}

export function listIntakeSessions(): IntakeSessionRecord[] {
  try {
    const raw = localStorage.getItem(INTAKE_SESSIONS_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item): item is IntakeSessionRecord => Boolean(item && item.id && Array.isArray(item.messages)))
      .sort((a, b) => b.updatedAt - a.updatedAt)
  } catch {
    return []
  }
}

export function saveIntakeSession(data: {
  id?: string
  slots: IntakeSlots
  messages: IntakeMessage[]
  firstMessage: string
  generationId?: string | null
}): IntakeSessionRecord | null {
  const hasUserMessage = data.messages.some((m) => m.role === 'user')
  if (!hasUserMessage && !data.slots.city && !data.firstMessage) {
    return null
  }

  const sessions = listIntakeSessions()
  const id = data.id || `session-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
  const title = deriveSessionTitle(data.slots, data.firstMessage)
  const record: IntakeSessionRecord = {
    id,
    title,
    updatedAt: Date.now(),
    slots: { ...data.slots },
    messages: [...data.messages],
    firstMessage: data.firstMessage,
    generationId: data.generationId ?? null,
  }

  const existingIndex = sessions.findIndex((s) => s.id === id)
  if (existingIndex >= 0) {
    sessions[existingIndex] = record
  } else {
    sessions.unshift(record)
  }

  // 最多保留 10 条草稿
  const trimmed = sessions.slice(0, 10)
  try {
    localStorage.setItem(INTAKE_SESSIONS_STORAGE_KEY, JSON.stringify(trimmed))
  } catch {
    // ignore
  }
  return record
}

export function deleteIntakeSession(id: string): void {
  const sessions = listIntakeSessions().filter((s) => s.id !== id)
  try {
    localStorage.setItem(INTAKE_SESSIONS_STORAGE_KEY, JSON.stringify(sessions))
  } catch {
    // ignore
  }
}

export function clearAllIntakeSessions(): void {
  try {
    localStorage.removeItem(INTAKE_SESSIONS_STORAGE_KEY)
  } catch {
    // ignore
  }
}

export function formatSessionTime(timestamp: number): string {
  const diff = Date.now() - timestamp
  if (diff < 60 * 1000) return '刚刚'
  if (diff < 60 * 60 * 1000) return `${Math.floor(diff / (60 * 1000))} 分钟前`
  if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / (60 * 60 * 1000))} 小时前`
  const date = new Date(timestamp)
  return `${date.getMonth() + 1}月${date.getDate()}日`
}
