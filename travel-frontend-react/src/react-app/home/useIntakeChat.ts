import { useCallback, useEffect, useRef, useState } from 'react'
import { clarifyItinerary, isOfflineError, isUnauthorized } from '../../api/sinan'
import type { IntakeState as ContractIntakeState } from '../../types/generated/contracts'
import {
  clearIntake,
  GREETING,
  loadIntake,
  mergeSlots,
  saveIntake,
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

/** M2（spec §7.2）表单改动后的重验话术：内容固定，携当前 slots+serverState 发给
 * /clarify，就绪与否只看响应的 ready/blocked（后端权威），不做本地推导。 */
const RECONFIRM_MESSAGE = '我更新了行程信息，请重新确认'

/** 对话式创建的会话状态机：每轮 POST /clarify 累积槽位，ready 后交确认条。
 * M2 起就绪状态是后端权威：ready 只在本轮响应 ready && !blocked 时落位；
 * 初始化/恢复会话/表单修改一律不落 ready，也不再用 slotsReady 本地推导。 */
export function useIntakeChat() {
  const [restored] = useState<IntakeState | null>(() => loadIntake())
  const [messages, setMessages] = useState<IntakeMessage[]>(() => restored?.messages ?? [GREETING])
  const [slots, setSlots] = useState<IntakeSlots>(() => restored?.slots ?? {})
  const [firstMessage, setFirstMessage] = useState(() => restored?.firstMessage ?? '')
  // M2：ready 只信后端——仅当本轮 clarify 响应 ready && !blocked 时置位。
  // 恢复的会话未经验证一律回对话态重新确认（spec §7.2「不由槽位齐了自动解锁」）。
  const [ready, setReady] = useState(false)
  // M2：表单修改后就绪失效标记——reconfirm 重验成功（ready 恢复）前不解锁生成
  const [needsReconfirm, setNeedsReconfirm] = useState(false)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  // 未登录撞上 clarify 的 401：生成跳有「登录后即可开始规划」的引导，clarify 这
  // 一跳此前只会弹「请求失败（401）」——未登录用户收集到一半就卡死（2026-09-30
  // 漏斗实测）。needsLogin 让确认区就地给出登录入口；会话已落 localStorage，
  // 登录回来恢复后接着聊。
  const [needsLogin, setNeedsLogin] = useState(false)
  const controller = useRef<AbortController | null>(null)
  // M2 迟到响应守卫：send/updateSlots/reset/restoreSession 都自增 seq；clarify 响应
  // 回来时 seq 对不上 = 期间用户又发过消息或改过表单，本轮响应（slots/state/ready/
  // 消息）整体丢弃，不得覆盖新修改。
  const seq = useRef(0)
  // ask 要读「当前」槽位（重验与迟到守卫语义下不能依赖渲染闭包快照），用 ref 镜像，
  // 一切写槽位的路径（ask 合并响应 / updateSlots / restore / reset）同步维护。
  const slotsRef = useRef<IntakeSlots>(restored?.slots ?? {})
  // M1a：服务端权威累计状态随轮往返——上一轮响应的 state 原样回传，需求
  // （必去/排除/节奏等 patch 结果）跨轮累积不丢；会话记录里同步持久化
  const serverState = useRef<ContractIntakeState | null>(
    (restored?.serverState as ContractIntakeState | undefined) ?? null,
  )

  useEffect(() => () => controller.current?.abort(), [])

  useEffect(() => {
    const isFresh = messages.length === 1 && messages[0].id === GREETING.id && !firstMessage
    if (isFresh) clearIntake()
    else saveIntake({ messages, slots, firstMessage, serverState: serverState.current ?? undefined })
  }, [messages, slots, firstMessage])

  /** 每轮 clarify 的公共通道（用户消息与表单重验共用）：返回本轮后端是否就绪。
   * 迟到响应（seq 过期）与被 abort 的请求一律按未就绪处理且不落任何状态。 */
  const ask = useCallback(async (rawText: string): Promise<boolean> => {
    const trimmed = rawText.trim()
    if (!trimmed || controller.current) return false
    setError('')
    setNeedsLogin(false)
    setSending(true)
    const askController = (controller.current = new AbortController())
    const mySeq = ++seq.current
    setMessages((list) => [...list, { id: nextMessageId(), role: 'user', text: trimmed }])
    setFirstMessage((current) => current || trimmed)

    try {
      const res = await clarifyItinerary(trimmed, { ...slotsRef.current }, serverState.current, askController.signal)
      // 迟到响应：期间用户又发过消息或改过表单 → 整体丢弃（消息流也不追加）
      if (seq.current !== mySeq) return false
      serverState.current = res.state
      const merged = mergeSlots(slotsRef.current, res.slots)
      slotsRef.current = merged
      setSlots(merged)
      // ready 权威在后端：ready && !blocked 才算就绪（blocked 硬阻断，任何路径不可绕过）
      const nowReady = Boolean(res.ready && !res.blocked)
      setReady(nowReady)
      if (nowReady) setNeedsReconfirm(false)
      // M2：助手话术 = reply（自然回复，优先）?? question（追问）；两者皆空且无
      // options 时不追加消息。客户端关键词推断与就绪模板已删（语义由后端承担）。
      const text = res.reply ?? res.question
      if ((text && text.trim()) || res.options.length) {
        setMessages((list) => [...list, {
          id: nextMessageId(),
          role: 'assistant',
          text: text ?? '',
          options: res.options.length ? [...res.options] : undefined,
        }])
      }
      return nowReady
    } catch (err) {
      if (askController.signal.aborted) return false
      if (isUnauthorized(err)) {
        setNeedsLogin(true)
        setError('登录后继续规划，你已填的想法会保留。')
        // F5：这句就是「最近一条用户消息」，暂存给登录回跳后的挂载续发；
        // 再撞 401 会以最新一条覆盖（续发永远补发最后一次想发的话）
        savePendingMessage(trimmed)
        return false
      }
      setError(
        isOfflineError(err)
          ? '暂时连不上规划服务，稍后再说一句试试。'
          : err instanceof Error
            ? err.message
            : '没听清，再说一次试试。',
      )
      return false
    } finally {
      if (controller.current === askController) controller.current = null
      setSending(false)
    }
  }, [])

  const send = useCallback((text: string) => ask(text), [ask])

  // F5 挂载续发：只在挂载时跑一次（send 已是无闭包依赖的稳定引用）。
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
  }, [send])

  /** 确认卡表单编辑（M2）：内容一改，后端就绪作废——ready 落 false、标记待重验；
   * 顺带自增 seq 让在途 clarify 响应整体作废（迟到响应不得覆盖新修改）。 */
  const updateSlots = useCallback((patch: Partial<IntakeSlots>) => {
    seq.current += 1
    const merged = { ...slotsRef.current, ...patch }
    slotsRef.current = merged
    setSlots(merged)
    setReady(false)
    setNeedsReconfirm(true)
  }, [])

  /** 后端重验（M2）：发一条固定话术携当前 slots+serverState，响应 ready 恢复才
   * 解锁生成；未就绪/失败返回 false，调用方保持开工禁用。 */
  const reconfirm = useCallback(() => ask(RECONFIRM_MESSAGE), [ask])

  const reset = useCallback(() => {
    seq.current += 1
    controller.current?.abort()
    controller.current = null
    serverState.current = null
    slotsRef.current = {}
    clearIntake()
    clearPendingMessage()
    setMessages([GREETING])
    setSlots({})
    setFirstMessage('')
    setReady(false)
    setNeedsReconfirm(false)
    setError('')
    setNeedsLogin(false)
    setSending(false)
  }, [])

  const restoreSession = useCallback((record: {
    messages: IntakeMessage[]
    slots: IntakeSlots
    firstMessage: string
    generationId?: string | null
    serverState?: ContractIntakeState
  }) => {
    seq.current += 1
    controller.current?.abort()
    controller.current = null
    serverState.current = record.serverState ?? null
    slotsRef.current = record.slots
    setMessages(record.messages)
    setSlots(record.slots)
    setFirstMessage(record.firstMessage)
    // 恢复会话不落 ready（M2）：回对话态继续聊，由后端重新验证后才解锁生成
    setReady(false)
    setNeedsReconfirm(false)
    setError('')
    setNeedsLogin(false)
    setSending(false)
    saveIntake({
      messages: record.messages,
      slots: record.slots,
      firstMessage: record.firstMessage,
      serverState: record.serverState,
    })
    if (record.generationId) {
      try {
        sessionStorage.setItem('sinan-intake-generation', record.generationId)
      } catch {
        // ignore
      }
    }
  }, [])

  // serverState 为 ref 快照：只在「点开始」瞬间读取传给生成请求，不驱动渲染
  return {
    messages,
    slots,
    firstMessage,
    ready,
    needsReconfirm,
    sending,
    error,
    needsLogin,
    send,
    updateSlots,
    reconfirm,
    reset,
    restoreSession,
    serverState: serverState.current,
  }
}

export type IntakeChat = ReturnType<typeof useIntakeChat>
