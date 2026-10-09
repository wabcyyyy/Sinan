import type { ItineraryStreamEvent } from '../../api/sinan'
import type { ItineraryDetail, TripItem } from '../../types/itinerary'

/**
 * M5a 逐项候选预览（spec §10.2）：业务面 item_preview / item_preview_withdrawn 帧
 * 的消费侧状态与纯 reducer。业务面进度 SSE 不入跨语言契约（帧表真源在
 * BE app/services/generation_events.py），FE 以开放 ItineraryStreamEvent 消费，
 * 本文件按消费键把开放 data 收窄成视图类型（同 NightlyBreakdownRow 先例）。
 *
 * 语义（spec §10.2 拍板）：
 * - 候选身份 = 服务端 previewId（"runId:dayNo:itemOrdinal"），重复帧不重复追加；
 * - runId 变化 = 换了 run：旧 run 残余候选整体作废，旧 run 的迟到帧一律忽略；
 * - 正式 day 快照是权威内容，整日替换候选（业务面的快照经轮询对账到达，
 *   见 useHomePlanning.monitor 的 prune；候选不逐条合并进正式条目）；
 * - 淘汰候选显式撤回，按 previewId 精确移除，不能静默变成另一地点；
 * - 预览只在生成进行中存在：终态清残余，页面重建即消失（不要求复活）。
 */

/** 候选条目对象：开放形状（未过 ground，坐标/关键事实保持未知），视图层只承诺
 * 「渲染键可能缺省」，缺键走兜底文案。形状派生自生成契约 TripItem 的可选视图。 */
export type PreviewItem = Partial<TripItem>

export interface ItemPreviewCandidate {
  /** 服务端身份 "runId:dayNo:itemOrdinal"：去重与撤回的唯一键 */
  previewId: string
  runId: string
  dayNo: number
  itemOrdinal: number
  item: PreviewItem
  /** B7：day_preview_mapping 帧对上的正式条目 id——候选已并入行程（快照到达
   * 前的过渡态如实呈现），没对上的候选等快照整日替换/撤回。 */
  officialItemId?: number
}

export interface ItemPreviewState {
  /** 当前 run：候选只从它追加。null=还没见过候选帧 */
  runId: string | null
  /** 见过的 run 登记：换 run 后旧 run 的迟到帧按「陈旧 run」忽略，不回滚现状 */
  seenRuns: string[]
  candidates: ItemPreviewCandidate[]
}

export function emptyPreviewState(): ItemPreviewState {
  return { runId: null, seenRuns: [], candidates: [] }
}

/** seenRuns 防御性上限：一次生成里的 run 数是个位数（整趟腿 + 兜底），超限裁最老。 */
const SEEN_RUNS_CAP = 8

const readString = (data: Record<string, unknown>, key: string): string =>
  typeof data[key] === 'string' ? (data[key] as string) : ''

const readNumber = (data: Record<string, unknown>, key: string): number =>
  typeof data[key] === 'number' && Number.isFinite(data[key]) ? (data[key] as number) : 0

/** 候选身份兜底拼装：服务端正常必带 previewId，缺键时按 BE 同一口径本地拼
 * （f"{runId}:{dayNo}:{itemOrdinal}"），宁可本地定位也不丢帧。 */
function previewIdOf(data: Record<string, unknown>, runId: string): string {
  const explicit = readString(data, 'previewId')
  return explicit || `${runId}:${readNumber(data, 'dayNo')}:${readNumber(data, 'itemOrdinal')}`
}

/** 业务面候选帧 → 预览状态（纯函数，独立可测）。其余帧原样返回不消费。 */
export function reduceItemPreviewEvent(state: ItemPreviewState, event: ItineraryStreamEvent): ItemPreviewState {
  const data = event.data ?? {}
  if (event.type === 'item_preview') {
    const runId = readString(data, 'runId')
    if (!runId) return state
    if (state.runId !== null && state.runId !== runId) {
      // 已登记的旧 run → 陈旧帧，忽略（不回滚当前 run 的候选）；
      // 全新 run → 旧 run 残余候选整体作废后切换（缺口天由正式快照/兜底重生成覆盖）
      if (state.seenRuns.includes(runId)) return state
      const seenRuns = [...state.seenRuns, state.runId].slice(-SEEN_RUNS_CAP)
      const fresh: ItemPreviewState = { runId, seenRuns, candidates: [] }
      return appendCandidate(fresh, data, runId)
    }
    return appendCandidate(state, data, runId)
  }
  if (event.type === 'item_preview_withdrawn') {
    const runId = readString(data, 'runId')
    // 只认当前 run 的撤回：旧 run 的撤回帧随其候选一并作废，撤不着也无需撤
    if (!runId || runId !== state.runId) return state
    const previewId = previewIdOf(data, runId)
    if (!state.candidates.some((candidate) => candidate.previewId === previewId)) return state
    return { runId: state.runId, seenRuns: state.seenRuns, candidates: state.candidates.filter((candidate) => candidate.previewId !== previewId) }
  }
  if (event.type === 'day_preview_mapping') {
    // B7（M5a 遗留收口，spec §10.2「持久化后提供 previewId→itemId 映射」）：
    // 正式落库后按内容身份对配。幸存候选挂 officialItemId（徽标换「已并入行程」）；
    // 没对上的 = 被后处理淘汰，缺席即如实，等快照整日替换。只认当前 run 的帧。
    if (readString(data, 'runId') !== state.runId) return state
    const entries = data.mappings
    if (!Array.isArray(entries) || entries.length === 0) return state
    const itemIdByPreviewId = new Map<string, number>()
    for (const raw of entries) {
      if (!raw || typeof raw !== 'object') continue
      const entry = raw as Record<string, unknown>
      const previewId = readString(entry, 'previewId')
      const itemId = readNumber(entry, 'itemId')
      if (previewId && itemId > 0) itemIdByPreviewId.set(previewId, itemId)
    }
    if (itemIdByPreviewId.size === 0) return state
    let changed = false
    const candidates = state.candidates.map((candidate) => {
      const itemId = itemIdByPreviewId.get(candidate.previewId)
      if (itemId === undefined || candidate.officialItemId === itemId) return candidate
      changed = true
      return { ...candidate, officialItemId: itemId }
    })
    return changed ? { runId: state.runId, seenRuns: state.seenRuns, candidates } : state
  }
  return state
}

function appendCandidate(state: ItemPreviewState, data: Record<string, unknown>, runId: string): ItemPreviewState {
  const previewId = previewIdOf(data, runId)
  if (state.candidates.some((candidate) => candidate.previewId === previewId)) {
    // 首帧前 runId 尚未确立（null），去重命中也要落 runId，否则后续撤回/换 run 判定失效
    return state.runId === runId ? state : { runId, seenRuns: state.seenRuns, candidates: state.candidates }
  }
  const candidate: ItemPreviewCandidate = {
    previewId,
    runId,
    dayNo: readNumber(data, 'dayNo'),
    itemOrdinal: readNumber(data, 'itemOrdinal'),
    item: (data.item ?? {}) as PreviewItem,
  }
  return { runId, seenRuns: state.seenRuns, candidates: [...state.candidates, candidate] }
}

/** 正式快照整日替换：轮询对账（waitForItinerary.onUpdate）的 draft 里已有条目的天，
 * 该天候选整体让位——快照权威，不做逐条合并。 */
export function prunePreviewsByDraft(state: ItemPreviewState, draft: ItineraryDetail | null): ItemPreviewState {
  if (!draft || state.candidates.length === 0) return state
  const doneDays = new Set(draft.dayList.filter((day) => day.items.length > 0).map((day) => day.dayNo))
  if (doneDays.size === 0) return state
  const candidates = state.candidates.filter((candidate) => !doneDays.has(candidate.dayNo))
  return candidates.length === state.candidates.length ? state : { runId: state.runId, seenRuns: state.seenRuns, candidates }
}

/** 某天的候选（按 itemOrdinal 升序，渲染顺序稳定）。 */
export function candidatesForDay(candidates: ItemPreviewCandidate[], dayNo: number): ItemPreviewCandidate[] {
  return candidates
    .filter((candidate) => candidate.dayNo === dayNo)
    .sort((a, b) => a.itemOrdinal - b.itemOrdinal)
}
