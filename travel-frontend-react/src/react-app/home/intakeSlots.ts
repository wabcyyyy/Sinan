/** 对话式创建的槽位模型与派生逻辑（纯函数，零依赖可单测）。
 *
 * 创建会话是短生命周期客户端状态：对话记录与槽位投影由前端持有（sessionStorage），
 * 服务端 /clarify 仍不落库；M1a 起需求的权威累计状态（serverState，契约 IntakeState）
 * 由服务端每轮返回、前端原样回传，语义合并不再由客户端重复做。键名与后端抽取
 * 口径一致（snake_case）。刷新可续，关闭标签页即弃——不做无壳草稿
 * （itinerary_chat_message 外键指向 itinerary）。
 */
import type { GenerateInput } from '../../api/sinan'
import type { IntakeState as ContractIntakeState, TripRequirements } from '../../types/generated/contracts'
import { inspirationTemplates } from '../data'

export const INTAKE_STORAGE_KEY = 'sinan-intake-v1'

/** M2（spec §7.2）会话存储版本：saveIntake 写入带 v 标记的记录，loadIntake 校验——
 * 缺版本或版本不符的旧记录一律返回 null（=会话重开，重新走一次确认），
 * 不由「槽位齐了」自动解锁。 */
export const INTAKE_STORAGE_VERSION = 2

export const INTAKE_PREFERENCES = ['美食', '自然风光', '人文历史', '慢节奏', '少走路', '亲子友好']

export interface IntakeSlots {
  city?: string
  days?: number
  persons?: number
  start_date?: string
  origin_city?: string
  budget?: number
  hotel_tier?: string
  preferences?: string[]
}

export interface IntakeMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  options?: string[]
}

export interface IntakeState {
  messages: IntakeMessage[]
  slots: IntakeSlots
  firstMessage: string
  /** M1a：服务端权威累计状态（契约 IntakeState 原样透传存储）；刷新续会话不丢需求 */
  serverState?: ContractIntakeState
}

export const GREETING: IntakeMessage = {
  id: 'greeting',
  role: 'assistant',
  text: '想去哪儿玩？大致天数、人数，一并说给司南听～',
}

function isDayCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 7
}

function isPersonCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 20
}

/** 槽位元数据（PLAN 2026-10-02 §3.2）：右栏收集进度与就绪门的单一来源。
 * 必填三项与后端 clarify 的 _REQUIRED 同口径，键名与后端抽取一致（snake_case）。 */
export interface SlotDef {
  key: keyof IntakeSlots
  label: string
  required: boolean
}

export const SLOT_DEFS: SlotDef[] = [
  { key: 'city', label: '目的地', required: true },
  { key: 'days', label: '天数', required: true },
  { key: 'persons', label: '人数', required: true },
  { key: 'start_date', label: '出发日期', required: false },
  { key: 'origin_city', label: '出发城市', required: false },
  { key: 'budget', label: '全程预算', required: false },
  { key: 'hotel_tier', label: '酒店档次', required: false },
  { key: 'preferences', label: '旅行偏好', required: false },
]

/** 单槽位是否已填：与 slotsReady 同一套判据（city 去空白、days/persons 走数值门）。 */
export function slotIsFilled(slots: IntakeSlots, key: keyof IntakeSlots): boolean {
  switch (key) {
    case 'city': return Boolean(slots.city?.trim())
    case 'days': return isDayCount(slots.days)
    case 'persons': return isPersonCount(slots.persons)
    case 'start_date': return Boolean(slots.start_date)
    case 'origin_city': return Boolean(slots.origin_city?.trim())
    case 'budget': return typeof slots.budget === 'number' && slots.budget > 0
    case 'hotel_tier': return Boolean(slots.hotel_tier?.trim())
    case 'preferences': return Boolean(slots.preferences?.length)
  }
}

/** 就绪门：从 SLOT_DEFS 的必填项派生（=后端 _REQUIRED 三件套），days 上限 7 不在前端放宽。 */
export function slotsReady(slots: IntakeSlots): boolean {
  return SLOT_DEFS.filter((def) => def.required).every((def) => slotIsFilled(slots, def.key))
}

export interface SlotProgress {
  requiredFilled: number
  requiredTotal: number
  filledKeys: Array<keyof IntakeSlots>
  nextRequiredKey: keyof IntakeSlots | null
}

/** 右栏收集进度：必填计数 + 已填键 + 下一个待填必填键（可选项不算门）。 */
export function slotProgress(slots: IntakeSlots): SlotProgress {
  const required = SLOT_DEFS.filter((def) => def.required)
  return {
    requiredFilled: required.filter((def) => slotIsFilled(slots, def.key)).length,
    requiredTotal: required.length,
    filledKeys: SLOT_DEFS.filter((def) => slotIsFilled(slots, def.key)).map((def) => def.key),
    nextRequiredKey: required.find((def) => !slotIsFilled(slots, def.key))?.key ?? null,
  }
}

/** 槽位当前值的人话展示（右栏「越填越长出来」的值面）：数字带单位、预算带币符。 */
export function slotDisplayValue(slots: IntakeSlots, key: keyof IntakeSlots): string {
  switch (key) {
    case 'city': return slots.city?.trim() || ''
    case 'days': return isDayCount(slots.days) ? `${slots.days} 天` : ''
    case 'persons': return isPersonCount(slots.persons) ? `${slots.persons} 人` : ''
    case 'start_date': return slots.start_date || ''
    case 'origin_city': return slots.origin_city?.trim() || ''
    case 'budget': return slots.budget && slots.budget > 0 ? `¥${slots.budget}` : ''
    case 'hotel_tier': return slots.hotel_tier?.trim() || ''
    case 'preferences': return (slots.preferences || []).join(' · ')
  }
}

/** 合并后端抽取结果：只认得动的键，形状不对的一律丢弃（槽位结构由前端决定）。 */
export function mergeSlots(current: IntakeSlots, incoming: Partial<IntakeSlots> | Record<string, unknown> | undefined | null): IntakeSlots {
  const next: IntakeSlots = { ...current }
  if (!incoming) return next
  if (typeof incoming.city === 'string' && incoming.city.trim()) next.city = incoming.city.trim().slice(0, 80)
  if (typeof incoming.origin_city === 'string' && incoming.origin_city.trim()) {
    next.origin_city = incoming.origin_city.trim().slice(0, 80)
  }
  if (typeof incoming.start_date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(incoming.start_date)) {
    next.start_date = incoming.start_date
  }
  if (typeof incoming.hotel_tier === 'string' && incoming.hotel_tier.trim()) next.hotel_tier = incoming.hotel_tier
  const days = toInt(incoming.days)
  if (days !== null) next.days = days
  const persons = toInt(incoming.persons)
  if (persons !== null) next.persons = persons
  const budget = toInt(incoming.budget)
  if (budget !== null && budget > 0) next.budget = budget
  if (Array.isArray(incoming.preferences)) {
    const items = incoming.preferences.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
    if (items.length) next.preferences = items.slice(0, 8)
  }
  return next
}

function toInt(value: unknown): number | null {
  const n = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN
  return Number.isFinite(n) ? Math.trunc(n) : null
}

/** 槽位 → 生成请求（调用前已过 slotsReady 门，此处数值兜底）。
 * stayNights=天数-1、endDate 随天数推导，与旧表单口径一致；
 * intent 带首句原话——建壳随 V10 落库，恢复续跑时读得回；
 * requirementsStruct 带 clarify 累积的结构化需求——建壳随 V13 落库（M1a）。 */
export function toGenerateInput(
  slots: IntakeSlots,
  firstMessage: string,
  serverRequirements?: TripRequirements | null,
): GenerateInput {
  const days = Math.min(Math.max(Math.trunc(slots.days || 0), 1), 7)
  const start = slots.start_date
  return {
    city: (slots.city || '').trim().slice(0, 80),
    days,
    persons: Math.min(Math.max(Math.trunc(slots.persons || 0), 1), 20),
    stayNights: Math.max(days - 1, 0),
    startDate: start || undefined,
    endDate: start ? addDays(start, days - 1) : undefined,
    originCity: slots.origin_city?.trim().slice(0, 80) || undefined,
    budget: slots.budget && slots.budget > 0 ? slots.budget : undefined,
    intent: firstMessage.trim().slice(0, 800) || undefined,
    preferences: (slots.preferences || []).filter((item) => item.trim() !== ''),
    hotelTier: slots.hotel_tier || undefined,
    requirementsStruct: serverRequirements ?? undefined,
  }
}

function addDays(isoDate: string, offset: number): string {
  const [y, m, d] = isoDate.split('-').map(Number)
  const date = new Date(y, (m || 1) - 1, (d || 1) + offset)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function loadIntake(): IntakeState | null {
  try {
    const raw = sessionStorage.getItem(INTAKE_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { v?: unknown } & Partial<IntakeState>
    // M2 版本校验：缺版本（旧格式）或版本不符 → 不恢复，调用方落回对话态重新确认
    if (parsed.v !== INTAKE_STORAGE_VERSION) return null
    if (!Array.isArray(parsed.messages)) return null
    const messages = parsed.messages.filter(
      (item): item is IntakeMessage =>
        Boolean(item) && (item.role === 'user' || item.role === 'assistant') && typeof item.text === 'string',
    )
    if (!messages.length) return null
    return {
      messages: messages.map((item, index) => ({
        id: item.id || `restored-${index}`,
        role: item.role,
        text: item.text,
        options: Array.isArray(item.options) ? item.options.filter((opt) => typeof opt === 'string') : undefined,
      })),
      slots: mergeSlots({}, parsed.slots),
      firstMessage: typeof parsed.firstMessage === 'string' ? parsed.firstMessage : '',
      serverState:
        parsed.serverState && typeof parsed.serverState === 'object'
          ? (parsed.serverState as ContractIntakeState)
          : undefined,
    }
  } catch {
    return null
  }
}

export function saveIntake(state: IntakeState): void {
  try {
    sessionStorage.setItem(INTAKE_STORAGE_KEY, JSON.stringify({ v: INTAKE_STORAGE_VERSION, ...state }))
  } catch {
    // 存储不可用（隐私模式等）：对话退化为页内存态，刷新重来可接受
  }
}

export function clearIntake(): void {
  try {
    sessionStorage.removeItem(INTAKE_STORAGE_KEY)
  } catch {
    // ignore
  }
}

/** 入口预填首句草稿：攻略页「按这座城市规划」（/?city=）、灵感模板（/?template=）
 * 与直链 ?intent= 都汇到这里；只预填不自动发送（StrictMode 双挂载不双发）。 */
export function seedFromQuery(query: URLSearchParams): string {
  const intent = (query.get('intent') || '').trim()
  if (intent) return intent.slice(0, 800)
  const template = inspirationTemplates.find((item) => item.id === query.get('template'))
  if (template) return template.intent.slice(0, 800)
  const city = (query.get('city') || '').trim()
  if (!city) return ''
  const days = Number(query.get('days'))
  const dayText = Number.isInteger(days) && days >= 1 && days <= 7 ? `玩 ${days} 天` : ''
  return `想去${city.slice(0, 40)}${dayText}，帮我安排一下`
}
