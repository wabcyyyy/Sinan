import { useCallback, useEffect, useRef, useState } from 'react'
import {
  generateItinerary,
  getItineraryDetail,
  isOfflineError,
  isUnauthorized,
  newIdempotencyToken,
  streamItineraryEvents,
  waitForItinerary,
} from '../../api/sinan'
import type { GenerateInput } from '../../api/sinan'
import type { ItineraryStreamEvent } from '../../api/sinan'
import type { ItineraryDetail } from '../../types/itinerary'
import { emptyPreviewState, prunePreviewsByDraft, reduceItemPreviewEvent } from './itemPreviews'
import type { ItemPreviewState } from './itemPreviews'

export type PlanningStatus = 'idle' | 'creating' | 'planning' | 'ready' | 'pending' | 'error' | 'login'

// 生成中的行程 id 暂存：刷新/断流后重挂载时据此续上监控（行程本身在服务端继续生成）；
// done 态也保留（F4，PLAN 2026-10-03 §2.4）——刷新后 resume 靠它拉详情恢复 ready+draft，
// 唯一清空点是会话重置（reset），中途清掉就会落回 confirm 态、可一键重复生成。
const GENERATION_KEY = 'sinan-intake-generation'

function readGenerationId(): number | null {
  try {
    const id = Number(sessionStorage.getItem(GENERATION_KEY))
    return Number.isInteger(id) && id > 0 ? id : null
  } catch {
    return null
  }
}

function saveGenerationId(id: number) {
  try {
    sessionStorage.setItem(GENERATION_KEY, String(id))
  } catch {
    // 存储不可用：只损失刷新续看，不影响本次生成
  }
}

function clearGenerationId() {
  try {
    sessionStorage.removeItem(GENERATION_KEY)
  } catch {
    // ignore
  }
}

// ===== M6（spec §12 创建入口）：attempt 幂等记录 =====
// 首次 POST **前**就落 sessionStorage：{ attemptId（X-Idempotency-Key 值）, hash（规范化
// 请求指纹）, phase, itineraryId }。同一次网络失败/响应丢失/刷新后的重试复用同一 key
//（后端同键 TTL 内只建壳一次，回放同一行程——「创建已接受但响应丢失仍只有一条行程」）；
// 请求实质变化（hash 不一致）或用户明确「重新说」（reset 清记录）才换 key。已拿到
// itineraryId 的恢复只走轮询（GENERATION_KEY resume），不再次提交。storage 不可用时
// 降级为模块级内存变量——会话内幂等仍成立，跨刷新不可恢复（如实限制）。
const ATTEMPT_KEY = 'sinan-intake-attempt'

interface AttemptRecord {
  attemptId: string
  hash: string
  phase: 'pending' | 'submitted'
  itineraryId: number | null
}

let memoryAttempt: AttemptRecord | null = null

function parseAttempt(raw: string): AttemptRecord | null {
  try {
    const value = JSON.parse(raw) as Partial<AttemptRecord> | null
    if (value && typeof value.attemptId === 'string' && value.attemptId && typeof value.hash === 'string') {
      return {
        attemptId: value.attemptId,
        hash: value.hash,
        phase: value.phase === 'submitted' ? 'submitted' : 'pending',
        itineraryId:
          typeof value.itineraryId === 'number' && Number.isInteger(value.itineraryId) && value.itineraryId > 0
            ? value.itineraryId
            : null,
      }
    }
  } catch {
    // 记录损坏：当作没有，按新 attempt 走
  }
  return null
}

/** 存储可写性探测：原值回写（Safari 旧隐私模式/配额满是「可读不可写」）。
 * 只在存储里没有可用记录时调用，用户动作频率，成本可忽略。 */
function attemptStorageWritable(): boolean {
  try {
    sessionStorage.setItem(ATTEMPT_KEY, sessionStorage.getItem(ATTEMPT_KEY) ?? '')
    return true
  } catch {
    return false
  }
}

function readAttempt(): AttemptRecord | null {
  try {
    const raw = sessionStorage.getItem(ATTEMPT_KEY)
    if (raw) return parseAttempt(raw)
  } catch {
    return memoryAttempt
  }
  // 存储可读但无记录：能写则以存储为准（外部清理/测试重置不复活内存影子），
  // 不能写（写失败场景）降级内存影子保会话内幂等
  return attemptStorageWritable() ? null : memoryAttempt
}

function writeAttempt(record: AttemptRecord | null) {
  memoryAttempt = record
  try {
    if (record === null) sessionStorage.removeItem(ATTEMPT_KEY)
    else sessionStorage.setItem(ATTEMPT_KEY, JSON.stringify(record))
  } catch {
    // 写不进：内存影子已保会话内幂等；跨刷新恢复能力如实受限（下次读取经探测走内存）
  }
}

const ATTEMPT_HASH_KEYS = [
  'city', 'days', 'persons', 'stayNights', 'budget', 'startDate', 'endDate',
  'originCity', 'intent', 'preferences', 'hotelTier', 'regionHint', 'requirements', 'requirementsStruct',
] as const

/** 规范化请求指纹：固定键序浅 stringify（缺字段补 null）后 FNV-1a + 长度。
 * 只在本会话内自比，不需要密码学强度；宁可比出不一致换新 key，不可错复用旧 key。 */
function attemptHash(input: GenerateInput): string {
  const source = input as unknown as Record<string, unknown>
  const canonical = ATTEMPT_HASH_KEYS.map((key) => `${key}=${JSON.stringify(source[key] ?? null)}`).join('|')
  let h = 0x811c9dc5
  for (let i = 0; i < canonical.length; i += 1) {
    h ^= canonical.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return `${(h >>> 0).toString(16)}:${canonical.length}`
}

/** 提交前认领 attempt：同 hash 复用原 key（失败/丢响应/刷新后的重试同键）；
 * hash 变了或没有记录才生成新 key。落盘发生在 POST 之前（这是同键重放成立的前提）。 */
function claimAttempt(hash: string): AttemptRecord {
  const existing = readAttempt()
  if (existing && existing.hash === hash) {
    const record: AttemptRecord = { ...existing, phase: 'pending' }
    writeAttempt(record)
    return record
  }
  const record: AttemptRecord = { attemptId: newIdempotencyToken(), hash, phase: 'pending', itineraryId: null }
  writeAttempt(record)
  return record
}

function settleAttempt(attemptId: string, itineraryId: number) {
  const existing = readAttempt()
  if (existing && existing.attemptId === attemptId) {
    writeAttempt({ ...existing, phase: 'submitted', itineraryId })
  }
}

/** 结果已知的失败（终态 FAILED/建壳被拒）后清 attempt：用户重试该拿到新一次尝试，
 * 而不是被回放到同一个失败壳。与 clearGenerationId 分开用——resume 拉不到详情属于
 *「结果未知」，只清 generationId，attempt 保留让下次提交复用键把行程找回来。 */
function clearAttempt() {
  writeAttempt(null)
}

export function useHomePlanning() {
  const [status, setStatus] = useState<PlanningStatus>('idle')
  const [message, setMessage] = useState('')
  const [progress, setProgress] = useState(0)
  const [draft, setDraft] = useState<ItineraryDetail | null>(null)
  // M5a 逐项候选（spec §10.2）+ M5b core_ready 里程碑（spec §11）：只存在于
  // 生成进行中，页面重建即消失（spec 明说不要求复活，DB 快照是对账权威）
  const [previewState, setPreviewState] = useState<ItemPreviewState>(emptyPreviewState)
  const [coreReady, setCoreReady] = useState(false)
  const request = useRef<AbortController | null>(null)
  const busy = status === 'creating' || status === 'planning'

  useEffect(() => () => request.current?.abort(), [])

  const monitor = useCallback(async (created: ItineraryDetail, controller: AbortController) => {
    const onEvent = (event: ItineraryStreamEvent) => {
      if (controller.signal.aborted) return
      const data = event.data || {}
      if (event.type === 'research_start') { setProgress(1); setMessage('正在查找目的地信息') }
      if (event.type === 'research_done') { setProgress(2); setMessage('正在安排每天的路线') }
      if (event.type === 'day_start') { setProgress(2); setMessage(`正在安排第 ${String(data.dayNo || '')} 天`) }
      if (event.type === 'day_done') setMessage(`第 ${String(data.dayNo || '')} 天已安排好`)
      if (event.type === 'butler_note') { setProgress(3); setMessage('正在补充出行提醒') }
      // M5a：候选发布/撤回（开放 data → itemPreviews 纯 reducer 收窄：previewId
      // 去重、换 run 作废、撤回精确移除）
      if (event.type === 'item_preview' || event.type === 'item_preview_withdrawn') {
        setPreviewState((prev) => reduceItemPreviewEvent(prev, event))
      }
      // M5b：core_ready 只是非阻断里程碑提示（「主行程可查看」），done 的完整
      // 收尾语义不变；stage_timing 是观测帧，前端暂不展示，透传不消费即可
      if (event.type === 'core_ready') setCoreReady(true)
      // 终态（complete 直发帧 / 重连后补的 done 快照帧）：清残余预览与里程碑提示
      if (event.type === 'complete' || event.type === 'done') {
        setPreviewState(emptyPreviewState()); setCoreReady(false)
      }
    }
    // SSE 与轮询并行：SSE 管进度文案，轮询 2s 对账管预览数据——串行的话，
    // 流没关之前 day_done 已落库但 setDraft 不更新，预览天卡会一直停在「安排中」。
    // 轮询超时须覆盖整个生成时长（默认 120s 是串行时代的余量），超限转 pending 兜底。
    const sse = streamItineraryEvents(created.id, controller.signal, { onEvent, onError: () => {} }).catch(() => {})
    // AICHAIN-2：跟踪最后一次对账状态，失败时区分「生成终态失败」与「通道瞬断/超时」——
    // 只有前者才清刷新续看标记；后者保留 id，刷新后 resumePending 仍能续看同一行程
    let lastStatus = created.status
    try {
      const result = await waitForItinerary(created.id, {
        signal: controller.signal,
        timeoutMs: 30 * 60 * 1000,
        onUpdate: (detail) => {
          if (!controller.signal.aborted) {
            setDraft(detail)
            // M5a：正式 day 快照（轮询对账到达）是权威内容——已有条目的天候选整日让位
            setPreviewState((prev) => prunePreviewsByDraft(prev, detail))
            // M6（spec §12）：coreReady 随详情投影到达——刷新/断流后轮询对账也能亮
            // 「主行程已可查看」提示，不再只依赖一次性的 SSE core_ready 帧
            if (detail.coreReady) setCoreReady(true)
            lastStatus = detail.status
          }
        },
      })
      if (controller.signal.aborted) return
      // done 不清 generationId：刷新后走 resume 恢复同一 ready 呈现（见 GENERATION_KEY 注）
      // 完整收尾（轮询通道先于 SSE 终态帧到达是常态）：残余预览与里程碑提示一并清
      setDraft(result); setPreviewState(emptyPreviewState()); setCoreReady(false)
      if (result.genState === 'FAILED') {
        // M6：gen_state=FAILED 是终态但不是成功——如实呈现失败与已得内容；
        // attempt 记录一并清（结果已知，重试拿新一次尝试而不是回放失败壳）
        clearGenerationId()
        clearAttempt()
        setStatus('pending')
        setMessage(result.planNote || '行程未能全部生成，已保留当前内容，可稍后重试。')
      } else {
        setProgress(4); setStatus('ready')
        // M6：PARTIAL（有 PENDING/FAILED 天）不得当成"已全部完成"
        setMessage(
          result.genState === 'PARTIAL'
            ? '行程主要内容已生成，部分天尚未完成，可在详情页重试未完成的天'
            : '你的行程已准备好',
        )
      }
    } catch (error) {
      if (controller.signal.aborted) return
      if (lastStatus === 3) {
        clearGenerationId()
        clearAttempt()
      }
      setStatus('pending')
      setMessage(error instanceof Error ? error.message : '进度暂时不可用，已保留当前行程。')
    }
    await sse
  }, [])

  async function submit(input: GenerateInput) {
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setStatus('creating'); setMessage('正在创建你的行程'); setProgress(0); setDraft(null)
    setPreviewState(emptyPreviewState()); setCoreReady(false)
    // M6（spec §12）：POST **前**认领并持久化 attempt——之后的网络失败/丢响应/刷新重试
    // 复用同一键（后端回放同一行程）；请求实质变化才换新键。只提交一次的责任由
    // 后端同键回放兜底，这里不靠按钮禁用。
    const attempt = claimAttempt(attemptHash(input))
    let created: ItineraryDetail | null = null
    try {
      created = await generateItinerary(input, attempt.attemptId, controller.signal)
      if (controller.signal.aborted) return
      saveGenerationId(created.id)
      settleAttempt(attempt.attemptId, created.id)
      setDraft(created); setStatus('planning')
      await monitor(created, controller)
    } catch (error) {
      if (controller.signal.aborted) return
      if (isUnauthorized(error)) {
        setStatus('login'); setMessage('登录后即可开始规划，你填写的想法会保留。')
      } else if (created) {
        setStatus('pending'); setMessage('进度暂时不可用，已保留当前行程，可从我的行程继续查看。')
      } else {
        setStatus('error'); setMessage(isOfflineError(error) ? '暂时无法连接规划服务。你的想法已保留，请稍后重试。' : error instanceof Error ? error.message : '这次规划没有完成，请重试。')
      }
    } finally {
      if (request.current === controller) request.current = null
    }
  }

  /** 刷新/重挂载后续看生成中的行程：SSE 不可达也能靠轮询对账。
   * resumedRef 保证只续一次（StrictMode 双挂载不重放）；controller 在首拍详情
   * 落地**之后**才挂到 request.current（FEUX-8）：挂早了会被 StrictMode 的模拟
   * 卸载掐死；挂晚了轮询开始前真卸载照样能被卸载 abort / reset 掐断，SPA 内
   * 路由切换后不再 30 分钟后台空转。 */
  const resumedRef = useRef(false)
  const resumePending = useCallback(async () => {
    if (resumedRef.current) return
    resumedRef.current = true
    const id = readGenerationId()
    if (!id) return
    const controller = new AbortController()
    try {
      const shell = await getItineraryDetail(id)
      if (!request.current) request.current = controller
      if (shell.status === 3) {
        // 终态失败是结果已知：resume 标记与 attempt 记录一并清，重试拿新一次尝试
        clearGenerationId()
        clearAttempt()
        return
      }
      setDraft(shell)
      if (shell.status === 2) {
        // 刷新恢复 done 态：与刚生成完的 ready 同一呈现分支（TripPanel 的 TripBoard），
        // id 继续保留——再刷新仍能恢复；清空只发生在会话重置。
        // M6：PARTIAL 也是 status==2 终态，但不是"已全部完成"——如实区分文案
        setProgress(4); setStatus('ready')
        setMessage(
          shell.genState === 'PARTIAL'
            ? '行程主要内容已生成，部分天尚未完成，可在详情页重试未完成的天'
            : '你的行程已准备好',
        )
        return
      }
      saveGenerationId(id)
      setStatus('planning'); setMessage('上次的规划还在进行，接着看'); setProgress(2)
      await monitor(shell, controller)
    } catch {
      // 只有无 abort 的真失败才清标记；abort（导航离开）保留续看入口
      if (!controller.signal.aborted) clearGenerationId()
    } finally {
      if (request.current === controller) request.current = null
    }
  }, [monitor])

  useEffect(() => { void resumePending() }, [resumePending])

  /** 会话重置（「重新说」链路，HomeStudio.handleChatReset 调用）：done 态保留的
   * generationId 在这里清——重置后的刷新不会被 resume 拉回旧行程，下次生成必是新行程。
   * 生成态与草稿一并归零，重置解锁后右栏不残留旧预览板；abort 兜底在位请求
   * （当前 reset 只在非 busy 态可达，正常为空）。 */
  const reset = useCallback(() => {
    request.current?.abort()
    request.current = null
    clearGenerationId()
    clearAttempt()
    setDraft(null); setProgress(0); setMessage(''); setStatus('idle')
    setPreviewState(emptyPreviewState()); setCoreReady(false)
  }, [])

  return {
    status, message, progress, draft,
    /** M5a：当前候选条目（按天渲染「正在完善」，仅生成进行中非空） */
    previews: previewState.candidates,
    /** M5b：主行程已可查看、备选仍在完善的非阻断提示态 */
    coreReady,
    busy, submit, reset,
  }
}
