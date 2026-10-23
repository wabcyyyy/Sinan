/** 展示专用数据入口。只响应固定脚本，永不转发到真实 API 或模型。 */
import type { ItineraryDetail, ItinerarySummary } from '../../types/itinerary'
import type { ItineraryChatMessage } from '../../types/chat'
import { makeDemoTrip, makeTemplateTrip, recalculate, relaxedPlans } from './fixtures'
import { CURATED_ITINERARIES } from '../../types/generated/contracts'
import type { ItineraryVersionVO } from '../../types/generated/contracts'
import { scenarioFixture } from './scenarioFixture'
import { createDemoItem, editDemoItem } from './itemAction'
import type { ItemActionBody, ItemCreateBody } from '../../types/generated/contracts'

const KEY = 'sinan-demo:trips-v1'
interface RecordState { trip: ItineraryDetail; startedAt?: number; messages: ItineraryChatMessage[]; revision: number; versions?: Array<ItineraryVersionVO & { snapshot: ItineraryDetail }>; scenarioMessageId?: number }
let memory: Record<string, RecordState> | undefined

function records(): Record<string, RecordState> {
  if (memory) return memory
  try { memory = JSON.parse(sessionStorage.getItem(KEY) || 'null') || undefined } catch { /* 新会话使用内置样例。 */ }
  return memory ||= { '101': { trip: makeDemoTrip(), messages: [], revision: 1 } }
}

function save() {
  try { sessionStorage.setItem(KEY, JSON.stringify(records())) } catch { /* 禁用存储时仅保持当前页面内存。 */ }
}

/** 每次引导使用独立合成副本，保留已经编辑的演示行程。 */
export function createEditTour(): number {
  const id = Math.max(200, ...Object.keys(records()).map(Number)) + 1
  records()[String(id)] = { trip: makeDemoTrip(id), messages: [], revision: 1 }
  save()
  return id
}

function snapshot(record: RecordState): ItineraryDetail {
  const total = record.trip.dayList.length
  const completed = record.startedAt === undefined ? total : Math.min(total, Math.floor((Date.now() - record.startedAt) / 2200))
  const done = completed === total
  return recalculate({ ...structuredClone(record.trip), planningRevision: record.revision, status: done ? 2 : 1, genState: done ? 'COMPLETED' : 'GENERATING', qualityStatus: done ? record.trip.qualityStatus : 'DRAFT',
    dayList: record.trip.dayList.map((day, index) => index < completed ? structuredClone(day)
      : { ...structuredClone(day), items: [], generationStatus: index === completed ? 'RUNNING' : 'PENDING' }) })
}

function json(data: unknown, status = 200, message = 'success'): Response {
  return new Response(JSON.stringify({ code: status, message, data }), { status, headers: { 'Content-Type': 'application/json' } })
}

function remember(record: RecordState, operation: string, summary: string) {
  const versions = record.versions ||= []
  const previous = versions.at(-1)
  const id = (previous?.id ?? 0) + 1
  versions.push({ id, versionNo: id, parentVersionId: previous?.id ?? null, operation, summary, createdAt: new Date().toISOString(), snapshot: snapshot(record) })
  return id
}

function delay(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('请求已取消', 'AbortError')); return }
    const abort = () => { clearTimeout(timer); reject(new DOMException('请求已取消', 'AbortError')) }
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, ms)
    signal?.addEventListener('abort', abort, { once: true })
  })
}

function stream(frames: Array<{ type: string; data?: unknown }>, signal?: AbortSignal | null): Response {
  let cancelled = false
  const body = new ReadableStream({
    async start(controller) {
      try {
        for (const frame of frames) {
          await delay(frame.type === 'day_done' ? 1100 : 180, signal)
          if (cancelled) return
          controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`))
        }
        if (!cancelled) controller.close()
      } catch (error) { if (!cancelled) controller.error(error) }
    },
    cancel() { cancelled = true },
  })
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } })
}

function clarify(body: { message?: string; slots?: Record<string, unknown> }): Response {
  const supported = body.message?.includes('成都') || body.slots?.city === '成都'
  const slots = { city: '成都', days: 2, persons: 2, budget: 1200, preferences: ['慢节奏', '美食'] }
  return json({ slots: supported ? slots : {}, ready: Boolean(supported), blocked: false, state: null,
    reply: supported ? '成都，两人两天一晚，预算 1200 元。先核对右侧信息，确认后开始安排。'
      : '这里展示真实产品页面，数据使用固定脚本。请选择“成都两天一晚”体验需求确认、逐日交付和对话编辑；其他城市仍可在探索页浏览。',
    options: supported ? [] : ['两个人去成都两天一晚，预算1200元，想慢慢逛'], question: null })
}

function edit(record: RecordState, text: string): ItineraryChatMessage {
  const relaxed = /第二天|第\s*2\s*天/.test(text) && /松|轻松|少|留白|草堂/.test(text)
    && record.trip.dayList[1]?.items.some((point) => point.itemType === 'attraction')
  const plans = relaxed ? relaxedPlans(record.trip) : undefined
  for (const msg of record.messages) { delete msg.plans; delete msg.baseRevision }
  const reply: ItineraryChatMessage = { id: Date.now(), role: 'ai',
    content: relaxed ? (record.trip.dayList[1].items.some((point) => point.poiName === '杜甫草堂')
      ? `第二天移除杜甫草堂，保留第一天与住宿。确认后才修改行程，费用小计减少 ${(record.trip.dayList[1].items.find((point) => point.poiName === '杜甫草堂')?.cost ?? 0) * record.trip.persons} 元。`
      : '建议第二天少安排一处景点，保留第一天与其他安排。先看看下方变化，再决定是否保存。')
      : '这是固定脚本展示，不会调用真实模型。你可以发送“把第二天安排松一点”，查看正式页面中的差异卡，并确认应用。',
    ...(plans ? { plans, baseRevision: String(record.revision), changed: true } : {}) }
  record.messages.push({ id: Date.now() - 1, role: 'user', content: text }, reply)
  save()
  return reply
}

export async function demoFetch(url: string, init: RequestInit = {}): Promise<Response> {
  if (init.signal?.aborted) throw new DOMException('请求已取消', 'AbortError')
  const target = new URL(url, 'https://demo.invalid')
  const path = target.pathname.replace(/^\/api/, '')
  const method = init.method || 'GET'
  const body = typeof init.body === 'string' ? JSON.parse(init.body) : {}
  if (path === '/user/info') return json({ id: 0, username: '演示访客', nickname: '演示访客', role: 'user' })
  if (path === '/templates' && method === 'GET') return json(CURATED_ITINERARIES.map(({ summary: _summary, ...card }) => card))
  const templateMatch = path.match(/^\/templates\/(-\d+)(\/fork)?$/)
  if (templateMatch) {
    const template = CURATED_ITINERARIES.find((row) => row.id === Number(templateMatch[1]))
    if (!template) return json(null, 404, '这条参考路线不存在')
    if (!templateMatch[2] && method === 'GET') return json(template)
    if (templateMatch[2] && method === 'POST') {
      const id = Math.max(200, ...Object.keys(records()).map(Number)) + 1
      const trip = makeTemplateTrip(template, id)
      records()[String(id)] = { trip, messages: [], revision: 1 }
      save()
      return json({ itineraryId: id, title: trip.title })
    }
  }
  if (path === '/itinerary/supported-cities') return json(['成都'])
  if (path === '/llm-gateway' && method === 'GET') return json([])
  if (path === '/user/trial-wallet') return json({ userId: 0, balanceCny: 0, spentCny: 0, entries: [] })
  if (path === '/itinerary/clarify') return clarify(body)
  if (path === '/itinerary/generate') {
    if (body.city !== '成都' || body.days !== 2 || body.persons !== 2 || (body.stayNights !== undefined && body.stayNights !== 1)) {
      return json(null, 400, '固定脚本仅提供成都两人两天一晚，请恢复这些信息后体验；其他需求需要连接真实后端。')
    }
    const trip = makeDemoTrip(102)
    trip.favorite = false
    trip.budget = body.budget ?? 1200
    records()['102'] = { trip, startedAt: Date.now(), messages: [], revision: 1 }
    save()
    return json(snapshot(records()['102']))
  }
  if (path === '/itinerary') {
    const view = target.searchParams.get('view')
    const query = target.searchParams.get('q') || ''
    const trips: ItinerarySummary[] = Object.values(records()).map((record) => {
      const trip = snapshot(record)
      return { ...trip, createdAt: '2026-10-16T09:00:00', archived: false, hasShare: false }
    })
    return json(trips.filter((trip) => (!query || `${trip.city}${trip.title}`.includes(query))
      && (view !== 'active' || trip.status === 1) && (view !== 'done' || trip.status === 2)))
  }
  const match = path.match(/^\/itinerary\/(\d+)(.*)$/)
  if (!match) return json(null, 400, '展示版未连接后端，此功能不执行真实操作；不会上传文件、保存密钥或产生费用。')
  const record = records()[match[1]]
  if (!record) return json(null, 404, '没有找到这条演示行程，请从“我的行程”重新打开。')
  const action = match[2]
  if (action === '/items/action' && method === 'POST') {
    if (body.planningRevision !== record.revision) return json(null, 409, '行程已更新，请刷新后再操作')
    try {
      const next = createDemoItem(snapshot(record), body as unknown as ItemCreateBody)
      const undoVersionId = remember(record, 'before_item', '新增安排前')
      record.trip = next; record.revision += 1
      for (const msg of record.messages) { delete msg.plans; delete msg.baseRevision }
      remember(record, 'item_action', '新增安排完成'); save()
      return json({ ...snapshot(record), undoVersionId })
    } catch (error) { return json(null, 400, error instanceof Error ? error.message : '添加失败') }
  }
  const itemAction = action.match(/^\/items\/(\d+)\/action$/)
  if (itemAction && method === 'POST') {
    if (body.planningRevision !== record.revision) return json(null, 409, '行程已更新，请刷新后再操作')
    try {
      const next = editDemoItem(snapshot(record), Number(itemAction[1]), body as unknown as ItemActionBody)
      const undoVersionId = remember(record, 'before_item', '条目修改前')
      record.trip = next; record.revision += 1
      for (const msg of record.messages) { delete msg.plans; delete msg.baseRevision }
      remember(record, 'item_action', '条目修改完成'); save()
      return json({ ...snapshot(record), undoVersionId })
    } catch (error) { return json(null, 400, error instanceof Error ? error.message : '修改失败') }
  }
  if (!action && method === 'GET') return json(snapshot(record))
  if (action === '/versions' && method === 'GET') return json([...(record.versions || [])].reverse().map(({ snapshot: _snapshot, ...version }) => version))
  const restoreMatch = action.match(/^\/versions\/(\d+)\/restore$/)
  if (restoreMatch && method === 'POST') {
    if (body.planningRevision !== record.revision || snapshot(record).status === 1) return json(null, 409, '行程已变化，请刷新后重新选择版本。')
    const version = record.versions?.find(version => version.id === Number(restoreMatch[1]))
    if (!version) return json(null, 404, '演示版本不存在')
    remember(record, 'before_restore', '恢复前的当前行程')
    record.trip = structuredClone(version.snapshot)
    record.revision += 1
    for (const msg of record.messages) { delete msg.plans; delete msg.baseRevision }
    remember(record, 'restore', `恢复到版本 ${version.versionNo}`)
    save()
    return json(snapshot(record))
  }
  if (action === '/scenarios/compare' || action === '/scenarios/select') {
    if (body.planningRevision !== record.revision) return json(null, 409, '行程已变化，请刷新后重新比较。')
    const original = makeDemoTrip().dayList[1].items
    const current = record.trip.dayList[1]?.items
    const supported = body.dayNo === 2 && record.trip.persons === 2 && body.startTime === '09:30' && body.finishBy === '16:00'
      && body.transferMinutes === 30 && body.departureBufferMinutes === 30 && JSON.stringify(body.fixedItemIds) === '[21]'
      && JSON.stringify(body.optionalItemIds) === '[24,25]' && current?.length === 5
      && original.every((item, index) => item.id === current[index].id && item.poiName === current[index].poiName && item.startTime === current[index].startTime && item.endTime === current[index].endTime)
    if (!supported) return json(null, 400, '请在未修改的成都第 2 天载入返程取舍示例；其他条件需连接真实后端。')
    const fixture = scenarioFixture(record.trip, record.revision)
    if (action.endsWith('/compare')) return json(fixture.comparison)
    if (body.choice !== 'omit') return json(null, 400, '保留全部安排存在时间冲突，不能应用。')
    for (const msg of record.messages) { delete msg.plans; delete msg.baseRevision }
    const id = Date.now()
    record.scenarioMessageId = id
    record.messages.push({ id, role: 'ai', content: '移除咖啡与晚餐，将宽窄巷子提前到 13:30。先确认差异，再保存这次调整。', plans: fixture.plans, changed: true, baseRevision: String(record.revision) })
    save()
    return json({ messageId: id, baseRevision: String(record.revision) })
  }
  if (action === '/weather') return json({ daily: [] })
  if (action === '/feedback') return json(null, 404, '展示版未启用反馈')
  if (action === '/events') return stream([
    { type: 'research_start' }, { type: 'research_done' },
    { type: 'day_start', data: { dayNo: 1 } }, { type: 'day_done', data: { dayNo: 1 } },
    { type: 'day_start', data: { dayNo: 2 } }, { type: 'day_done', data: { dayNo: 2 } }, { type: 'done' },
  ], init.signal)
  if (action === '/chat-history') return json(record.messages)
  if (action === '/chat-edit/stream' || action === '/chat-edit') {
    const reply = edit(record, String(body.message || ''))
    const payload = { ...reply, reply: reply.content, messageId: reply.id }
    return action.endsWith('/stream') ? stream([
      { type: 'chat_token', data: { delta: reply.content } }, { type: 'chat_draft', data: payload }, { type: 'chat_done' },
    ], init.signal) : json(payload)
  }
  if (action === '/apply-plans') {
    const draft = record.messages.find((msg) => msg.id === body.actionMessageId && msg.plans?.length)
    if (!draft || body.baseRevision !== String(record.revision)) return json(null, 409, '演示草稿已失效，请重新发送编辑指令。')
    const items = draft.plans?.[0]?.items
    if (!items) return json(null, 409, '演示草稿不完整，请重新生成建议。')
    const names = new Set(items.map((point) => point.poi_name))
    const fixed = record.trip.fixedArrangements || []
    if (fixed.some(f => f.dayNo === 2 && !items.some(p => p.poi_name === f.name && p.start_time?.slice(0, 5) === f.startTime && p.end_time?.slice(0, 5) === f.endTime))) return json(null, 409, '草稿改变了固定安排，请先解除固定或重新提出修改')
    const undoVersionId = remember(record, 'before_apply', '确认修改前的行程')
    record.trip.dayList[1].items = record.trip.dayList[1].items.filter((point) => names.has(point.poiName))
    if (draft.id === record.scenarioMessageId) {
      record.trip.dayList[1].items = record.trip.dayList[1].items.map(point => { const proposal = items.find(item => item.poi_name === point.poiName)!; return { ...point, startTime: proposal.start_time, endTime: proposal.end_time } })
      record.trip.dayList[1].note = '固定返程取舍示例：15:00 结束，转场与返程时长为演示假设。'
    } else record.trip.dayList[1].theme = '少赶一站，把上午留白'
    record.trip = recalculate(record.trip)
    record.revision += 1
    remember(record, 'apply', '已确认的修改')
    delete draft.plans
    delete draft.baseRevision
    record.messages.push({ id: Date.now(), role: 'ai', content: '修改已保存，第一天和住宿保持不变。' })
    save()
    return json({ ...snapshot(record), undoVersionId })
  }
  if (action === '/favorite') { record.trip.favorite = Boolean(body.favorite); save(); return json(snapshot(record)) }
  if (/^\/days\/\d+$/.test(action) && method === 'PATCH') {
    const day = record.trip.dayList.find((day) => day.dayId === Number(action.split('/')[2]))
    if (!day) return json(null, 404, '演示日期不存在')
    day.theme = String(body.theme).slice(0, 80)
    record.revision += 1
    save()
    return json(snapshot(record))
  }
  return json(null, 400, '展示版未连接后端，此功能需要真实服务；你可以体验浏览、收藏、编辑标题和“把第二天安排松一点”。')
}
