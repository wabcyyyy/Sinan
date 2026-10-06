import { useCallback, useEffect, useRef, useState } from 'react'
import { clarifyItinerary, isOfflineError, isUnauthorized } from '../../api/sinan'
import {
  assistantReply,
  clearIntake,
  extractPreferencesFromText,
  GREETING,
  guessDateFromText,
  isDatePending,
  isSkipOrDirectStart,
  loadIntake,
  mergeSlots,
  saveIntake,
  slotsReady,
} from './intakeSlots'
import type { IntakeMessage, IntakeSlots, IntakeState } from './intakeSlots'

let messageSeq = 0
function nextMessageId() {
  messageSeq += 1
  return `msg-${Date.now().toString(36)}-${messageSeq}`
}

/** F5 登录续发（PLAN 2026-10-03 §2.3）：未登录撞 clarify 401 时，把那句没送出去的
 * 话暂存 sessionStorage；登录回跳本 hook 重新挂载时若已登录 → 先清键再自动补发一次，
 * 未登录保留键等下次。键的读写各自兜 try/catch（同 intakeSlots 的存储纪律）。 */
const INTAKE_PENDING_KEY = 'sinan-intake-pending'
const LOGIN_STORAGE_KEY = 'sinan-username'

function savePendingMessage(text: string): void {
  try {
    sessionStorage.setItem(INTAKE_PENDING_KEY, text)
  } catch {
    // 存储不可用（隐私模式等）：丢的只是「自动补发」一步，对话本身照常
  }
}

function readPendingMessage(): string | null {
  try {
    return sessionStorage.getItem(INTAKE_PENDING_KEY)
  } catch {
    return null
  }
}

function clearPendingMessage(): void {
  try {
    sessionStorage.removeItem(INTAKE_PENDING_KEY)
  } catch {
    // ignore
  }
}

/** 对话式创建的会话状态机：每轮 POST /clarify 累积槽位，ready 后交确认条。
 * 就绪与否不落存储——恢复时从槽位重新推导，blocked 天数会自然跌回对话态。 */
export function useIntakeChat() {
  const [restored] = useState<IntakeState | null>(() => loadIntake())
  const [messages, setMessages] = useState<IntakeMessage[]>(() => restored?.messages ?? [GREETING])
  const [slots, setSlots] = useState<IntakeSlots>(() => restored?.slots ?? {})
  const [firstMessage, setFirstMessage] = useState(() => restored?.firstMessage ?? '')
  const [ready, setReady] = useState(() => slotsReady(restored?.slots ?? {}))
  const [dateAsked, setDateAsked] = useState(() => Boolean(restored?.slots?.start_date))
  const [prefsAsked, setPrefsAsked] = useState(() => Boolean(restored?.slots?.preferences?.length))
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  // 未登录撞上 clarify 的 401：生成跳有「登录后即可开始规划」的引导，clarify 这
  // 一跳此前只会弹「请求失败（401）」——未登录用户收集到一半就卡死（2026-09-30
  // 漏斗实测）。needsLogin 让确认区就地给出登录入口；会话已落 localStorage，
  // 登录回来恢复后接着聊。
  const [needsLogin, setNeedsLogin] = useState(false)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => controller.current?.abort(), [])

  useEffect(() => {
    const isFresh = messages.length === 1 && messages[0].id === GREETING.id && !firstMessage
    if (isFresh) clearIntake()
    else saveIntake({ messages, slots, firstMessage })
  }, [messages, slots, firstMessage])

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || sending) return
      setError('')
      setNeedsLogin(false)
      setSending(true)
      const ask = (controller.current = new AbortController())
      setMessages((list) => [...list, { id: nextMessageId(), role: 'user', text: trimmed }])
      if (!firstMessage) setFirstMessage(trimmed)

      // 自然语言直解与意图嗅探：
      const userSkipped = isSkipOrDirectStart(trimmed)
      const datePending = isDatePending(trimmed)
      const guessedDate = guessDateFromText(trimmed)
      const extractedPrefs = extractPreferencesFromText(trimmed)

      const clientPatches: Partial<IntakeSlots> = {}
      if (guessedDate && !slots.start_date) {
        clientPatches.start_date = guessedDate
      }
      if (extractedPrefs.length) {
        const existing = slots.preferences || []
        clientPatches.preferences = Array.from(new Set([...existing, ...extractedPrefs]))
      }

      let nextDateAsked = dateAsked
      let nextPrefsAsked = prefsAsked
      if (guessedDate || datePending || slots.start_date) {
        nextDateAsked = true
        setDateAsked(true)
      }
      if (extractedPrefs.length || userSkipped || (slots.preferences && slots.preferences.length)) {
        nextPrefsAsked = true
        setPrefsAsked(true)
      }

      const activeSlots = { ...slots, ...clientPatches }

      try {
        const res = await clarifyItinerary(trimmed, activeSlots, ask.signal)
        const merged = mergeSlots(activeSlots, res.slots)
        setSlots(merged)
        setReady(slotsReady(merged))
        const reply = assistantReply(res.ready, res.question ?? null, res.options, merged, {
          dateAsked: nextDateAsked,
          prefsAsked: nextPrefsAsked,
          userSkipped,
        })
        if (reply) setMessages((list) => [...list, { ...reply, id: nextMessageId() }])
      } catch (err) {
        if (ask.signal.aborted) return
        if (isUnauthorized(err)) {
          setNeedsLogin(true)
          setError('登录后继续规划，你已填的想法会保留。')
          // F5：这句就是「最近一条用户消息」，暂存给登录回跳后的挂载续发；
          // 再撞 401 会以最新一条覆盖（续发永远补发最后一次想发的话）
          savePendingMessage(trimmed)
          return
        }
        setError(
          isOfflineError(err)
            ? '暂时连不上规划服务，稍后再说一句试试。'
            : err instanceof Error
              ? err.message
              : '没听清，再说一次试试。',
        )
      } finally {
        if (controller.current === ask) controller.current = null
        setSending(false)
      }
    },
    [slots, sending, firstMessage],
  )

  // F5 挂载续发：只在挂载时跑一次（send 取初始闭包，带恢复出的槽位上下文补发）。
  // ① 幂等闸 = 「先清键」：StrictMode 双挂载第二遍键已没了，不双发；未登录则保留键。
  // ② send 挪进 setTimeout(0)：同 useHomePlanning.resumePending 的教训——挂载期 effect
  //    里发起的请求会被上方 abort 清理 effect 的 StrictMode 模拟卸载误杀（键已清、
  //    请求死掉 = 那句话真丢了），挪出本轮 commit 才发得出去；dev 双挂载与线上都只发一次。
  //    刻意不 clearTimeout：模拟卸载会顺带清掉它；真卸载后补发只是对已卸载组件
  //    多一次无害 setState，连接由浏览器自己回收（同 resumePending 口径）。
  useEffect(() => {
    const pending = readPendingMessage()
    if (!pending) return
    if (!localStorage.getItem(LOGIN_STORAGE_KEY)) return
    clearPendingMessage()
    window.setTimeout(() => void send(pending), 0)
  }, [])

  const updateSlots = useCallback(
    (patch: Partial<IntakeSlots>) => {
      const merged = { ...slots, ...patch }
      setSlots(merged)
      setReady(slotsReady(merged))
    },
    [slots],
  )

  const reset = useCallback(() => {
    controller.current?.abort()
    controller.current = null
    clearIntake()
    clearPendingMessage()
    setMessages([GREETING])
    setSlots({})
    setFirstMessage('')
    setReady(false)
    setDateAsked(false)
    setPrefsAsked(false)
    setError('')
    setNeedsLogin(false)
    setSending(false)
  }, [])

  const restoreSession = useCallback((record: {
    messages: IntakeMessage[]
    slots: IntakeSlots
    firstMessage: string
    generationId?: string | null
  }) => {
    controller.current?.abort()
    controller.current = null
    setMessages(record.messages)
    setSlots(record.slots)
    setFirstMessage(record.firstMessage)
    setReady(slotsReady(record.slots))
    setDateAsked(Boolean(record.slots.start_date))
    setPrefsAsked(Boolean(record.slots.preferences?.length))
    setError('')
    setNeedsLogin(false)
    setSending(false)
    saveIntake({ messages: record.messages, slots: record.slots, firstMessage: record.firstMessage })
    if (record.generationId) {
      try {
        sessionStorage.setItem('sinan-intake-generation', record.generationId)
      } catch {
        // ignore
      }
    }
  }, [])

  return { messages, slots, firstMessage, ready, sending, error, needsLogin, send, updateSlots, reset, restoreSession }
}

export type IntakeChat = ReturnType<typeof useIntakeChat>
