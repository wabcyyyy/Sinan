/**
 * 行程域视图类型。
 *
 * 与后端线级契约重叠的类型（FactEvidence/QualityReport/HotelOption/Suggestion 等）
 * 一律来自生成类型 types/generated/contracts.ts（G-1.1 单一源，手写副本已删除）；
 * 本文件只保留「业务详情 VO」（ORM + metadata_json 动态组装、后端无对应 schema）
 * 与少量对契约的视图层收窄/扩展。
 */
import type * as Contracts from './generated/contracts'

/**
 * 行程点位（视图层）。后端线级字段的类型一律派生自生成契约 Contracts.TripItem（单一真源，R5-1）：
 * itemType/poiName 必填，其余按「视图层全可选」不变量取 Partial（同文件 PhotoSpotEntry/DayOption 的口径），
 * 手写副本（曾各自重复声明 20 个字段）已删除。后端改字段名 → 这里 tsc 必红（故意留此耦合）。
 * 只补「业务详情 VO 才回传、契约里没有」的视图扩展键。
 */
type TripItemContractFields = Pick<Contracts.TripItem, 'itemType' | 'poiName'> &
  Partial<Omit<Contracts.TripItem, 'itemType' | 'poiName'>>

export interface TripItem extends TripItemContractFields {
  /** ORM 主键：详情 VO 回传，契约无此键 */
  id?: number
  /** 详情 VO 偶发回传的介绍/图说字段，契约 TripItem 里没有 */
  description?: string | null
  intro?: string | null
  /** 图片备用字段（契约只有 image） */
  imageUrl?: string | null
  /** ORM 排序号，详情 VO 回传 */
  sortNo?: number
}

/**
 * 逐日备选分支：契约字段（label/summary/tradeoff）来自生成类型；
 * items 在契约里是开放结构（unknown[]），视图层收窄为业务点位。
 */
export interface DayOption extends Contracts.DayOption {
  items: TripItem[]
}

/** 拍照点位条目（DayVO.photoSpots 为 Map 透传）：§4.1.2 PhotoSpot{name, tip, best_time}；
 * name/title 兼容历史数据（旧版仅名称）。历史行可能缺键，故**不**继承契约类型
 * （契约层全键必有，视图层须全可选）。 */
export interface PhotoSpotEntry {
  name?: string | null
  title?: string | null
  /** 拍摄角度 / 构图建议 */
  tip?: string | null
  /** 最佳时段（如 09:00-10:00，data mono 渲染） */
  bestTime?: string | null
}

/** 备选安排条目（DayVO.backupPlan 为 Map 透传）：§4.1.2 BackupRule{if, action}；
 * name/title 兼容历史数据（旧版仅名称）。同 PhotoSpotEntry：历史行缺键不继承契约。 */
export interface BackupPlanEntry {
  name?: string | null
  title?: string | null
  /** 触发条件（如「雨天或体力不足」，斜体衬线渲染） */
  if?: string | null
  /** 替换动作正文 */
  action?: string | null
}

export interface DayPlan {
  dayId: number
  dayNo: number
  /** 天级生成态（PENDING/RUNNING/SUCCEEDED/FAILED）：刷新/断线后按天显示「安排中」 */
  generationStatus?: string | null
  travelDate?: string | null
  note?: string | null
  items: TripItem[]
  theme?: string | null
  miniRoute?: Record<string, unknown>
  backupPlan?: BackupPlanEntry[]
  photoSpots?: PhotoSpotEntry[]
  practicalNotes?: string[]
  dayOptions?: DayOption[]
}

export interface BudgetRow {
  category: string
  amount: number
  itemCount: number
}

/** 封面署名（S1）：随图落库，详情/分享页展示用 */
export interface CoverCredit {
  author?: string | null
  authorUrl?: string | null
  license?: string | null
  source?: string | null
}

export interface ItineraryDetail {
  schemaVersion?: string
  id: number
  title: string
  city: string
  startDate?: string | null
  endDate?: string | null
  days: number
  stayNights: number
  persons: number
  budget?: number | null
  preferences?: string | null
  hotelTier?: string | null
  planNote?: string | null
  /** 叙事字段（M3 生成契约补齐后生效）：整趟主题标题 */
  tripTheme?: string | null
  /** 封面快照 / 收藏 / 归档 / 分享（S1/S2 后端已回传；详情是本人视角，含 shareToken） */
  coverUrl?: string | null
  coverSource?: string | null
  coverCredit?: CoverCredit | null
  favorite?: boolean
  archived?: boolean
  shareToken?: string | null
  /** 用户旅行意图原文（§5.3.1 意图回显行；后端暂未回传，有值才渲染） */
  intent?: string | null
  status: number
  /** M6（spec §12）：生成阶段只读投影——null/GENERATING/CORE_READY/COMPLETED/PARTIAL/FAILED。
   * waitForItinerary 据此区分「完全就绪」「部分完成（有 PENDING/FAILED 天）」与「核心可看」。 */
  genState?: 'CORE_READY' | 'COMPLETED' | 'PARTIAL' | 'FAILED' | 'GENERATING' | null
  /** M6：genState==CORE_READY 时为 true——主行程已可查看、备选仍在完善（轮询继续）。 */
  coreReady?: boolean
  /** 协作（C2.3）：当前调用者在本行程中的角色；owner/editor 可写，viewer 只读 */
  myRole?: 'owner' | 'editor' | 'viewer'
  /** 模板（C2.4）：已发布时间；有值=发布中（详情为本人视角才回传） */
  templatePublishedAt?: string | null
  dayList: DayPlan[]
  budgetList: BudgetRow[]
  totalAmount: number
  destinationStatus?: 'knowledge_backed' | 'researched' | 'draft_only'
  qualityStatus?: Contracts.QualityReport['qualityStatus']
  qualityRuleVersion?: string
  validatedAt?: string | null
  pendingFactCount?: number
  sources?: Contracts.SourceRecord[]
  qualityReport?: Contracts.QualityReport
  suggestions?: Contracts.Suggestion[]
}

export interface ItinerarySummary {
  id: number
  title: string
  city: string
  startDate?: string | null
  endDate?: string | null
  days: number
  persons: number
  budget?: number | null
  totalAmount: number
  status: number
  createdAt: string
  /** 整趟主题标题（§5.5 主题摘要行）：后端列表 VO（ItinerarySummaryVO）暂未携带该字段，
   * 也没有 dayList（首日 theme 不可得）；前端按「字段存在才渲染」防御式实现，
   * 后端补充 tripTheme 后列表卡片自动升级，无需改前端。 */
  tripTheme?: string | null
  /** S1/S2 后端已回传：封面三列 + 收藏/归档/分享态（列表只回 hasShare 布尔） */
  coverUrl?: string | null
  coverSource?: string | null
  coverCredit?: CoverCredit | null
  favorite?: boolean
  archived?: boolean
  hasShare?: boolean
}

/**
 * 酒店备选方案（/chat-edit 草稿与 /hotel-option 应用共用）。
 * 契约字段来自生成类型；baseRevision 是业务层 /hotel-option 应用的附加字段
 * （乐观并发指纹，不在 agent 契约内）。
 */
export interface HotelOption extends Contracts.HotelOption {
  baseRevision?: string | null
}

/** 每晚价格明细行：契约里是开放 dict，视图层按实际消费键收窄
 * （type 别名而非 interface：对象字面量类型才有隐式索引签名，可 narrowing 覆盖契约的 Record）。 */
export type NightlyBreakdownRow = {
  dayNo: number
  stayDate?: string | null
  seasonLabel: string
  seasonFactor: number
  nightlyPrice: number
}

export interface HotelRoomOption extends Contracts.HotelRoomOption {
  nightlyBreakdown: NightlyBreakdownRow[]
}

/** 酒店实时价行（LA2 按需通道，POST /hotel-quotes/live）：后端 live_hotel_quotes
 * 返回开放 dict，视图层按实际消费键收窄（NightlyBreakdownRow 同款先例）。 */
export type LiveHotelQuoteRow = {
  name: string
  nightlyPrice: number
  totalPrice?: number | null
  currency?: string | null
  source?: string | null
  rating?: number | null
}

/** /hotel-quotes/live 信封 data：城市+日期窗一律取行程自身（后端读库），行按每晚价升序。 */
export interface LiveHotelQuotes {
  hotelQuotes: LiveHotelQuoteRow[]
  city: string
  checkIn: string
  checkOut: string
  reason: string | null
}
