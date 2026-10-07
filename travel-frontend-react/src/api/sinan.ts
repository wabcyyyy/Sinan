import type * as Contracts from '../types/generated/contracts'
import type { ChatDraftPayload, ItineraryChatMessage } from '../types/chat'
import type { ItineraryDetail, ItinerarySummary, LiveHotelQuotes } from '../types/itinerary'

export class ReactApiError extends Error {
  status?: number
  code?: number

  constructor(message: string, status?: number, code?: number) {
    super(message)
    this.name = 'ReactApiError'
    this.status = status
    this.code = code
  }
}

type Envelope<T> = { code: number; message: string; data: T }

// 前缀单独成常量：apiRequest 是透传管道，具体路径由各调用点声明
// （后端 cutover 契约测试逐点扫描 apiRequest 调用，见 test_cutover_contract.py）。
const API_BASE = '/api'

async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      Accept: 'application/json',
      // FormData 不能手写 Content-Type：multipart boundary 由浏览器生成，写了反而发不出
      ...(init.body && !(init.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
      ...(init.headers ?? {}),
    },
  })

  const raw = await response.text()
  let payload: Envelope<T> | null = null
  if (raw) {
    try {
      payload = JSON.parse(raw) as Envelope<T>
    } catch {
      throw new ReactApiError('服务器返回了无法识别的内容', response.status)
    }
  }
  if (!response.ok) {
    throw new ReactApiError(payload?.message || `请求失败（${response.status}）`, response.status, payload?.code)
  }
  if (payload && typeof payload.code === 'number' && payload.code !== 200) {
    throw new ReactApiError(payload.message || '请求失败', response.status, payload.code)
  }
  return (payload?.data ?? payload) as T
}

function waitFor(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('请求已取消', 'AbortError'))
      return
    }
    let timer = 0
    const cleanup = () => signal?.removeEventListener('abort', onAbort)
    const onAbort = () => {
      window.clearTimeout(timer)
      cleanup()
      reject(new DOMException('请求已取消', 'AbortError'))
    }
    timer = window.setTimeout(() => { cleanup(); resolve() }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export function isOfflineError(error: unknown): boolean {
  return error instanceof TypeError || (error instanceof ReactApiError && (!error.status || error.status >= 500))
}

export function isUnauthorized(error: unknown): boolean {
  return error instanceof ReactApiError && error.status === 401
}

export function getSupportedCities() {
  return apiRequest<string[]>('/itinerary/supported-cities')
}

export function login(data: Contracts.LoginBody) {
  return apiRequest<Contracts.LoginData>('/auth/login', { method: 'POST', body: JSON.stringify(data) })
}

export function register(data: Contracts.RegisterBody) {
  return apiRequest<void>('/auth/register', { method: 'POST', body: JSON.stringify(data) })
}

export function logout() {
  return apiRequest<void>('/auth/logout', { method: 'POST' })
}

export function getUserInfo() {
  return apiRequest<Contracts.UserInfoVO>('/user/info')
}

export interface GenerateInput {
  city: string
  days: number
  persons: number
  stayNights: number
  budget?: number
  startDate?: string
  endDate?: string
  originCity?: string
  intent?: string
  preferences: string[]
  hotelTier?: string
  regionHint?: string
  requirements?: string
}

export function clarifyItinerary(message: string, slots: Record<string, unknown>, signal?: AbortSignal) {
  return apiRequest<Contracts.ClarifyResponse>('/itinerary/clarify', {
    method: 'POST',
    signal,
    body: JSON.stringify({ message, slots }),
  })
}

export function generateItinerary(data: GenerateInput, idempotencyKey: string, signal?: AbortSignal) {
  return apiRequest<ItineraryDetail>('/itinerary/generate', {
    method: 'POST',
    signal,
    headers: { 'X-Idempotency-Key': idempotencyKey },
    body: JSON.stringify(data),
  })
}

export function listItineraries(view?: string, query?: string) {
  const params = new URLSearchParams()
  if (view && view !== 'all') params.set('view', view)
  if (query) params.set('q', query)
  return apiRequest<ItinerarySummary[]>(`/itinerary?${params.toString()}`)
}

export function getItineraryDetail(id: number | string) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}`)
}

export function getItineraryWeather(id: number | string) {
  return apiRequest<Contracts.WeatherVO>(`/itinerary/${id}/weather`)
}

export interface WaitForItineraryOptions {
  signal?: AbortSignal
  intervalMs?: number
  timeoutMs?: number
  onUpdate?: (detail: ItineraryDetail) => void
}

/**
 * The generate endpoint creates a persisted shell before the background agent
 * fills each day. SSE is the preferred progress channel, but a page refresh or
 * a proxy can interrupt that stream. This bounded poll keeps the already
 * persisted draft recoverable without creating another itinerary.
 *
 * AICHAIN-2：单次轮询的瞬时网络失败不再立刻放弃——与 SSE 通道同预算的
 * 4 次指数退避重试（成功即重置），弱网下兜底通道不比主通道更脆。
 */
export async function waitForItinerary(id: number | string, options: WaitForItineraryOptions = {}) {
  const startedAt = Date.now()
  const intervalMs = Math.max(500, options.intervalMs ?? 2000)
  const timeoutMs = Math.max(intervalMs, options.timeoutMs ?? 120000)
  const maxTransientFailures = 4
  let transientFailures = 0

  for (;;) {
    let detail: ItineraryDetail
    try {
      detail = await apiRequest<ItineraryDetail>(`/itinerary/${id}`, { signal: options.signal })
    } catch (error) {
      if (options.signal?.aborted || !isOfflineError(error) || transientFailures >= maxTransientFailures) throw error
      transientFailures += 1
      await waitFor(intervalMs * 2 ** (transientFailures - 1), options.signal)
      continue
    }
    transientFailures = 0
    options.onUpdate?.(detail)
    if (detail.status === 2) return detail
    if (detail.status === 3) {
      throw new ReactApiError(detail.planNote || '行程生成失败，请稍后重试')
    }
    if (Date.now() - startedAt >= timeoutMs) {
      throw new ReactApiError('行程仍在后台整理，可以稍后从我的行程继续查看')
    }
    await waitFor(intervalMs, options.signal)
  }
}

export function getSharedItinerary(token: string) {
  return apiRequest<{
    city: string
    title: string
    days: number
    persons: number
    startDate: string | null
    endDate: string | null
    budget: number | null
    tripTheme: string | null
    totalAmount: number
    dayList: Array<{ dayNo: number; travelDate: string | null; theme: string | null; note: string | null; dayTotalAmount: number; items: Array<{ poiName: string; itemType: string; address: string | null; startTime: string | null; endTime: string | null; cost: number | null }> }>
    budgetList: Array<{ category: string; amount: number | null; itemCount: number }>
    planNote: string | null
  }>(`/share/${encodeURIComponent(token)}`)
}

export function optimizeDay(id: number | string, dayId: number) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}/optimize`, {
    method: 'POST',
    body: JSON.stringify({ dayId }),
  })
}

/** 重生成第 N 天（P0-3 部分交付自救）：真·LLM 重新生成未交付天（PENDING/FAILED/空），
 * 服务端只拦分钟窗不扣当日配额；已成功天会被 400 拒（那类调整走 optimizeDay/对话编辑）。 */
export function regenerateDay(id: number | string, dayNo: number) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}/days/${dayNo}/regenerate`, { method: 'POST' })
}

export function updateDay(id: number | string, dayId: number, theme: string) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}/days/${dayId}`, {
    method: 'PATCH',
    body: JSON.stringify({ theme }),
  })
}

export function setFavorite(id: number, favorite: boolean) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}/favorite`, {
    method: 'POST',
    body: JSON.stringify({ favorite }),
  })
}

// 条目对/错反馈（C3.5）：flag 关闭时三端点 404（隐藏能力），GET 兼作前端能力探针
export function listMyItemFeedback(id: number | string) {
  return apiRequest<Contracts.FeedbackListVO>(`/itinerary/${id}/feedback`)
}

export function submitItemFeedback(id: number | string, body: Contracts.FeedbackCreate) {
  return apiRequest<Contracts.FeedbackVO>(`/itinerary/${id}/feedback`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function revokeItemFeedback(id: number | string, itemId: number) {
  return apiRequest<null>(`/itinerary/${id}/feedback/${itemId}`, { method: 'DELETE' })
}

export function createShare(id: number, expireDays: 7 | 30 | null = 7) {
  return apiRequest<{ shareToken: string; shareUrl: string; shareExpiresAt: string | null }>(`/itinerary/${id}/share`, {
    method: 'POST',
    body: JSON.stringify({ expireDays }),
  })
}

export function createPdfExport(id: number) {
  return apiRequest<{ id: number; status: 'RUNNING' | 'DONE' | 'FAILED'; downloadUrl: string | null }>(`/export/pdf/${id}`, {
    method: 'POST',
  })
}

export function getExportTask(taskId: number) {
  return apiRequest<{ id: number; status: 'RUNNING' | 'DONE' | 'FAILED'; downloadUrl: string | null; errorMsg: string | null }>(`/export/tasks/${taskId}`)
}

export function exportDownloadUrl(taskId: number) {
  return `/api/export/download/${taskId}`
}

export interface WaitForExportOptions {
  signal?: AbortSignal
  intervalMs?: number
  timeoutMs?: number
  onUpdate?: (task: Awaited<ReturnType<typeof getExportTask>>) => void
}

export async function waitForExport(taskId: number, options: WaitForExportOptions = {}) {
  const startedAt = Date.now()
  const intervalMs = Math.max(500, options.intervalMs ?? 1500)
  const timeoutMs = Math.max(intervalMs, options.timeoutMs ?? 60000)
  for (;;) {
    const task = await getExportTask(taskId)
    options.onUpdate?.(task)
    if (task.status === 'DONE') return task
    if (task.status === 'FAILED') throw new ReactApiError(task.errorMsg || 'PDF 导出失败')
    if (Date.now() - startedAt >= timeoutMs) throw new ReactApiError('PDF 仍在生成，请稍后再试')
    await waitFor(intervalMs, options.signal)
  }
}

export type ItineraryStreamEvent = {
  type: string
  itineraryId?: number
  seq?: number
  ts?: string
  data?: Record<string, unknown>
  plan?: Contracts.DailyPlan
  message?: string
}

export interface StreamHandlers {
  onEvent: (event: ItineraryStreamEvent) => void
  onError: (error: Error) => void
}

/**
 * Subscribe to the existing itinerary SSE endpoint. The caller owns the
 * AbortController so a route change never leaves a stream running.
 */
export async function streamItineraryEvents(
  id: number | string,
  signal: AbortSignal,
  handlers: StreamHandlers,
  options: StreamReconnectOptions = {},
) {
  const maxReconnects = options.maxReconnects ?? 4
  const baseDelayMs = options.baseDelayMs ?? 1500
  let attempt = 0
  let lastError: Error | null = null
  for (;;) {
    // 终态帧之后的干净关流不重连：服务端对终态行程只补一帧快照就关，
    // 无脑重连会跟 snapshot 打成 ping-pong（每次都「连上→补帧→关流」）。
    let terminal = false
    const forwarding: StreamHandlers = {
      onEvent: (event) => {
        if (event.type === 'done' || event.type === 'error') terminal = true
        handlers.onEvent(event)
      },
      // 中间失败静默（后面可能重连成功），最终失败由本函数收口时回调
      onError: () => {},
    }
    try {
      await readItineraryStream(id, signal, forwarding)
      if (terminal || signal.aborted) return
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      if (signal.aborted) return
      // 4xx 是鉴权/归属问题，重试只会原样再吃一次；5xx 与网络错才值得续
      if (error instanceof ReactApiError && error.status && error.status < 500) {
        handlers.onError(error)
        throw error
      }
      lastError = error instanceof Error ? error : new Error('实时进度连接中断')
    }
    if (attempt >= maxReconnects) {
      const finalError = lastError || new Error('实时进度连接多次中断')
      handlers.onError(finalError)
      throw finalError
    }
    attempt += 1
    await waitFor(baseDelayMs * 2 ** (attempt - 1), signal)
  }
}

export interface StreamReconnectOptions {
  /** 断流自动重连次数上限（默认 4，指数退避 1.5s 起） */
  maxReconnects?: number
  baseDelayMs?: number
}

async function readItineraryStream(id: number | string, signal: AbortSignal, handlers: StreamHandlers) {
  const response = await fetch(`/api/itinerary/${id}/events`, {
    credentials: 'include',
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!response.ok || !response.body) {
    throw new ReactApiError(`实时进度暂不可用（${response.status}）`, response.status)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = frame
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .join('\n')
        if (data) {
          try {
            handlers.onEvent(JSON.parse(data) as ItineraryStreamEvent)
          } catch {
            // One malformed frame should not discard later progress.
          }
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return
    const normalized = error instanceof Error ? error : new Error('实时进度连接中断')
    handlers.onError(normalized)
    throw normalized
  } finally {
    reader.releaseLock()
  }
}

// ===== 行程对话（chat-edit，CH3/L2）=====

export type ChatHistory = Array<{ role: 'user' | 'ai'; content: string }>

export interface ChatReply extends ChatDraftPayload {
  reply?: string
}

export function getItineraryChatHistory(id: number | string) {
  return apiRequest<ItineraryChatMessage[]>(`/itinerary/${id}/chat-history`)
}

/** 阻塞版对话回合：流式失败时的回退通道。 */
export function chatEditItinerary(id: number | string, message: string, history: ChatHistory, signal?: AbortSignal) {
  return apiRequest<ChatReply>(`/itinerary/${id}/chat-edit`, {
    method: 'POST',
    signal,
    body: JSON.stringify({ message, history }),
  })
}

/**
 * 流式对话回合：POST SSE，帧形 {type, itineraryId, seq, ts, data}。
 * chat_token → onToken(delta)；chat_draft → onDraft(草稿九键，plans 为 snake_case)；
 * error 帧 → throw Error(message)；chat_done 忽略。
 */
export async function chatEditStream(
  id: number | string,
  message: string,
  history: ChatHistory,
  handlers: { onToken: (delta: string) => void; onDraft: (payload: ChatDraftPayload) => void },
  signal?: AbortSignal,
) {
  const response = await fetch(`/api/itinerary/${id}/chat-edit/stream`, {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history }),
    signal,
  })
  if (!response.ok || !response.body) {
    throw new ReactApiError(`流式连接不可用（${response.status}）`, response.status)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = frame
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trim())
          .join('\n')
        if (data) {
          let parsed: { type?: string; data?: Record<string, unknown>; message?: string } | null = null
          try {
            parsed = JSON.parse(data)
          } catch {
            // 脏帧不抛：一帧坏不该断掉整条流
          }
          if (!parsed) continue
          if (parsed.type === 'chat_token') handlers.onToken(String(parsed.data?.delta ?? ''))
          else if (parsed.type === 'chat_draft') handlers.onDraft((parsed.data ?? {}) as ChatDraftPayload)
          else if (parsed.type === 'error') {
            throw new Error(String(parsed.message || parsed.data?.message || '行程助手暂时不可用'))
          }
          // chat_done / heartbeat：无需处理
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') return
    throw error instanceof Error ? error : new Error('实时连接中断')
  } finally {
    reader.releaseLock()
  }
}

/** 应用对话草稿：服务端有意忽略 plans，只认 actionMessageId 对应的持久化草稿。 */
export function applyPlans(id: number | string, actionMessageId: number | null, baseRevision: string | null) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}/apply-plans`, {
    method: 'POST',
    body: JSON.stringify({ plans: [], actionMessageId, baseRevision }),
  })
}

export interface HotelApplyInput {
  hotelName: string
  tier: string
  roomType: string
  dayNos: number[]
}

/** 应用酒店候选：HITL 确认（replace_hotel）经此端点 resume。 */
export function applyHotelOption(
  id: number | string,
  input: HotelApplyInput,
  actionMessageId: number | null,
  baseRevision: string | null,
) {
  return apiRequest<ItineraryDetail>(`/itinerary/${id}/hotel-option`, {
    method: 'POST',
    body: JSON.stringify({ ...input, actionMessageId, baseRevision }),
  })
}

// ===== 酒店实时价（POST /itinerary/{id}/hotel-quotes/live，LA2/L16）=====

/** 按需查酒店实时价：城市与日期窗一律取行程自身（后端读库），不落库。
 * 429=按用户分钟窗限速（审查 P1-8，与航班实时价共用 SerpApi 月池）；
 * 400=行程缺城市/日期；200+空列表+reason=问过但没查到。 */
export function liveHotelQuotes(id: number | string, body?: Contracts.LiveQuotesBody | null) {
  return apiRequest<LiveHotelQuotes>(`/itinerary/${id}/hotel-quotes/live`, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  })
}

// ===== 客户端错误探针（POST /api/client-errors，匿名可达）=====

// 会话级防刷：同源同消息只报一次、总量封顶。上限刻意压在服务端分钟窗（30）之下，
// 正常页面崩溃（个位数错误）不受影响，失控循环才有截断。
const CLIENT_ERROR_SESSION_CAP = 20
const reportedErrors = new Set<string>()

/** fire-and-forget 上报：探针失败必须静默——它失败时再抛错会递归触发自身。 */
export function reportClientError(report: Contracts.ClientErrorReport): void {
  const key = `${report.source || 'unknown'}:${report.message}`
  if (reportedErrors.has(key) || reportedErrors.size >= CLIENT_ERROR_SESSION_CAP) return
  reportedErrors.add(key)
  void apiRequest<void>('/client-errors', {
    method: 'POST',
    // keepalive：崩溃/卸载场景下浏览器也要把这最后一发发出去
    keepalive: true,
    body: JSON.stringify(report),
  }).catch(() => {})
}

// ===== 图片意图理解（POST /image-intent，对话输入的图片腿）=====

/** 旅行相关图片 → 可直接进对话的一句话：multipart 上传（file + 可选 context 表单域），
 * 返回的 suggestedMessage 由调用方回填输入框交用户编辑后照常发送——不自动发送。 */
export function interpretImageIntent(file: File, context?: string) {
  const body = new FormData()
  body.append('file', file)
  if (context) body.append('context', context)
  return apiRequest<Contracts.ImageIntentVO>('/image-intent', { method: 'POST', body })
}

// ===== 语音意图理解（POST /speech-intent，对话输入的语音腿）=====

/** 录音 → 可直接进对话的一句话：multipart 上传（file），服务端走 LLM_ROLE_STT（MiMo）。
 * 与图片腿同口径：拿到的 suggestedMessage 回填输入框交用户编辑后照常发送——不自动发送。 */
export function interpretSpeechIntent(file: File) {
  const body = new FormData()
  body.append('file', file)
  return apiRequest<Contracts.SpeechIntentVO>('/speech-intent', { method: 'POST', body })
}

// ===== 模型接入设置（/llm-gateway，BYOK 管理面；设置页专用）=====

/** 配置列表：后端只回 apiKeyHint（尾 4 位），明文密钥永不回传。 */
export function listLlmGateways() {
  return apiRequest<Contracts.LlmGatewayVO[]>('/llm-gateway')
}

export function createLlmGateway(data: Contracts.LlmGatewayCreateBody) {
  return apiRequest<Contracts.LlmGatewayVO>('/llm-gateway', { method: 'POST', body: JSON.stringify(data) })
}

/** 更新：apiKey 传 null/空串 = 不改密钥，其余字段非空即覆盖。 */
export function updateLlmGateway(id: number, data: Contracts.LlmGatewayUpdateBody) {
  return apiRequest<Contracts.LlmGatewayVO>(`/llm-gateway/${id}`, { method: 'PUT', body: JSON.stringify(data) })
}

/** 删除启用中的配置视同同时停用（生效通道自然回到司南默认）。 */
export function deleteLlmGateway(id: number) {
  return apiRequest<null>(`/llm-gateway/${id}`, { method: 'DELETE' })
}

/** 启用互斥：启用这条会自动停用同账号的其他配置（单一生效通道）。 */
export function enableLlmGateway(id: number) {
  return apiRequest<Contracts.LlmGatewayVO>(`/llm-gateway/${id}/enable`, { method: 'POST' })
}

export function disableLlmGateway(id: number) {
  return apiRequest<Contracts.LlmGatewayVO>(`/llm-gateway/${id}/disable`, { method: 'POST' })
}

/** 连通性测试：后端经该配置发一条 1-token 消息，返回 ok/延迟/脱敏错误摘要。 */
export function testLlmGateway(id: number) {
  return apiRequest<Contracts.LlmGatewayTestVO>(`/llm-gateway/${id}/test`, { method: 'POST' })
}
