import type { HotelOption } from './itinerary'

/**
 * NL 编辑 / chat 域类型。
 * 后端草稿 plans 为 snake_case 开放结构，这里声明前端实际消费的最小字段
 * （day_no / items[].poi_name / start_time），替代此前的 any[] 逃逸。
 */

/** chat 草稿中的单个点位（后端返回 snake_case 草稿结构）。 */
export interface ChatPlanItem {
  poi_name?: string | null
  start_time?: string | null
  end_time?: string | null
  /** 未核实提示用：AI 新增但无坐标的点位视为「位置未核实」 */
  latitude?: number | null
  longitude?: number | null
}

/** chat 草稿中的单日方案。 */
export interface ChatDayPlan {
  day_no: number
  items?: ChatPlanItem[]
}

/**
 * HITL 确认提案（L2/CH3）：后端 pendingAction，内部键为 snake_case。
 * 当前唯一构造方是换酒店提案（chat_draft/hotel.py），type === 'replace_hotel'。
 */
export interface ChatPendingAction {
  type: string
  hotel_names?: string[]
  target_tier?: string | null
  day_numbers?: number[]
  night_count?: number
  requires_confirmation?: boolean
}

/** 一条对话消息（AI 消息可携带结构化草稿与住宿备选）。 */
export interface ItineraryChatMessage {
  id?: number
  role: 'user' | 'ai'
  content: string
  plans?: ChatDayPlan[]
  hotelOptions?: HotelOption[]
  changed?: boolean
  baseRevision?: string
  /** HITL：true 时需要用户在对话流内确认（当前=从酒店候选中点选）后才可应用 */
  requiresConfirmation?: boolean
  pendingAction?: ChatPendingAction | null
  createdAt?: string
}

/** chat 草稿推送载荷：除 reply 外与 /chat-edit 响应字段一致。 */
export interface ChatDraftPayload {
  reply?: string
  plans?: ChatDayPlan[]
  hotelOptions?: HotelOption[]
  changed?: boolean
  baseRevision?: string
  messageId?: number
  requiresConfirmation?: boolean
  pendingAction?: ChatPendingAction | null
}
