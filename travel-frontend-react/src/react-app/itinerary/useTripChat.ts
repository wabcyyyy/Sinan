import { useCallback, useEffect, useRef, useState } from 'react'
import {
  applyHotelOption,
  applyPlans,
  chatEditItinerary,
  chatEditStream,
  getItineraryChatHistory,
  newIdempotencyToken,
  ReactApiError,
} from '../../api/sinan'
import type { ChatHistory, ChatReply } from '../../api/sinan'
import type { ItineraryChatMessage } from '../../types/chat'
import type { HotelOption, ItineraryDetail } from '../../types/itinerary'
import { activeActionIndex } from './chatDraft'

const SEND_TIMEOUT_MS = 70000
const HISTORY_LIMIT = 12

/** M6（spec §12 编辑入口）：turn 幂等 409 的两种语义识别。
 * 「仍在处理」= 后端同一任务仍在跑（网络超时≠后台取消），引导等待、绝不重发第二份；
 * 「已用于不同请求」= 同 turnId 被别的请求占用，按错误透出。其余 409/错误返回 null。
 * 文案单一源在后端（信封 message），这里只做语义分类、不仿写第二份。 */
function turnConflictMessage(error: unknown): string | null {
  if (!(error instanceof ReactApiError) || error.status !== 409) return null
  const message = error.message || ''
  if (message.includes('仍在处理') || message.includes('已用于不同请求')) return message
  return null
}

function toHistory(msgs: ItineraryChatMessage[]): ChatHistory {
  return msgs
    .filter((msg) => msg.content.trim() && typeof msg.id === 'number' && msg.id > 0)
    .slice(-HISTORY_LIMIT)
    .map((msg) => ({ role: msg.role, content: msg.content }))
}

/** 对话编排状态机（CH3/L2）：流式发送 + 超时回退阻塞 + 旧草稿镜像清理 + 应用/酒店确认。
 * 行为基准是本目录单测（语义移植自已退役的旧版编辑面板 ChatEditPanel）；确认卡（requiresConfirmation/pendingAction）长在对话流内。 */
export function useTripChat(
  itineraryId: number,
  onApplied: (detail: ItineraryDetail) => void,
  onReconcile: () => void,
) {
  const [msgs, setMsgs] = useState<ItineraryChatMessage[]>([])
  const [loaded, setLoaded] = useState(false)
  const [sending, setSending] = useState(false)
  const [notice, setNotice] = useState('')
  const [applying, setApplying] = useState(false)
  const seq = useRef(0)
  const msgsRef = useRef(msgs)
  msgsRef.current = msgs

  const reload = useCallback(async () => {
    try {
      const history = await getItineraryChatHistory(itineraryId)
      setMsgs(history)
    } catch {
      setMsgs([])
    } finally {
      setLoaded(true)
    }
  }, [itineraryId])

  useEffect(() => {
    setLoaded(false)
    setNotice('')
    void reload()
  }, [reload])

  const activeIndex = activeActionIndex(msgs)
  const active = activeIndex >= 0 ? msgs[activeIndex] : null

  /** 草稿落地：清掉其它 AI 消息的草稿字段（「当前唯一待确认」），回填本轮 payload。 */
  const applyDraftPayload = useCallback(
    (aiLocalId: number, payload: ChatReply) => {
      const prevActive = activeActionIndex(msgsRef.current)
      const prevActiveMsg = prevActive >= 0 ? msgsRef.current[prevActive] : null
      const replaced = Boolean(
        prevActiveMsg && prevActiveMsg.id !== payload.messageId && (prevActiveMsg.plans?.length || prevActiveMsg.hotelOptions?.length),
      )
      setMsgs((list) =>
        list.map((msg) => {
          if (msg.id === aiLocalId) {
            return {
              ...msg,
              id: typeof payload.messageId === 'number' ? payload.messageId : msg.id,
              plans: payload.plans,
              hotelOptions: payload.hotelOptions,
              changed: payload.changed,
              baseRevision: payload.baseRevision,
              requiresConfirmation: payload.requiresConfirmation,
              pendingAction: payload.pendingAction ?? null,
            }
          }
          if (msg.role === 'ai' && (msg.plans?.length || msg.hotelOptions?.length)) {
            return { ...msg, plans: undefined, hotelOptions: undefined, changed: undefined, baseRevision: undefined, requiresConfirmation: undefined, pendingAction: null }
          }
          return msg
        }),
      )
      if (replaced) setNotice('本轮建议已替代上一份未应用方案')
    },
    [],
  )

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || sending) return
      setNotice('')
      setSending(true)
      const history = toHistory(msgsRef.current)
      const userId = -(++seq.current)
      const aiId = -(++seq.current)
      // M6（spec §12）：同一 send 只生成一次 turnId——流式与其内部回退的阻塞通道
      // 复用同一 turn 身份（后端 409 保护下断流恢复不会跑出第二份）；用户下一次
      // 主动发送才是新 turn。
      const turnId = newIdempotencyToken()
      setMsgs((list) => [
        ...list,
        { id: userId, role: 'user', content: trimmed },
        { id: aiId, role: 'ai', content: '' },
      ])
      const controller = new AbortController()
      const timer = window.setTimeout(() => controller.abort(), SEND_TIMEOUT_MS)
      const dropPlaceholder = () => setMsgs((list) => list.filter((msg) => msg.id !== aiId))
      /** 409 turn 冲突的就地呈现：「仍在处理」留占位（结果稍后经历史可见）+ 等待提示，
       * 「已用于不同请求」撤占位 + 错误透出；两类都不自动重发。返回是否按冲突处理。 */
      const showTurnConflict = (error: unknown): boolean => {
        const message = turnConflictMessage(error)
        if (!message) return false
        const stillProcessing = message.includes('仍在处理')
        setMsgs((list) =>
          stillProcessing
            ? list.map((msg) => (msg.id === aiId && !msg.content ? { ...msg, content: '（还在处理中，稍后即可看到结果）' } : msg))
            : list.filter((msg) => msg.id !== aiId),
        )
        setNotice(message)
        return true
      }
      try {
        await chatEditStream(
          itineraryId,
          trimmed,
          history,
          {
            onToken: (delta) =>
              setMsgs((list) => list.map((msg) => (msg.id === aiId ? { ...msg, content: msg.content + delta } : msg))),
            onDraft: (payload) => {
              applyDraftPayload(aiId, payload)
              setMsgs((list) => list.map((msg) => (msg.id === aiId ? { ...msg, content: msg.content || '好的，建议如下：' } : msg)))
            },
          },
          controller.signal,
          turnId,
        )
      } catch (error) {
        if (controller.signal.aborted) {
          setNotice('本轮处理超时，可直接重试或换个说法。')
          setMsgs((list) =>
            list.map((msg) => (msg.id === aiId && !msg.content ? { ...msg, content: '（这一轮没有等到回复）' } : msg)),
          )
        } else if (!showTurnConflict(error)) {
          // 流式失败回退阻塞端点（兜底）——同一 turnId：若后端原 turn 仍在跑，
          // 回退会拿到 409 并按冲突语义呈现，不会执行第二份
          try {
            const reply: ChatReply = await chatEditItinerary(itineraryId, trimmed, history, controller.signal, turnId)
            applyDraftPayload(aiId, reply)
            setMsgs((list) =>
              list.map((msg) => (msg.id === aiId ? { ...msg, content: msg.content || reply.reply || '好的，建议如下：' } : msg)),
            )
          } catch (fallbackError) {
            if (!showTurnConflict(fallbackError)) {
              dropPlaceholder()
              setNotice(
                fallbackError instanceof Error && fallbackError.message
                  ? fallbackError.message
                  : '这条没太理解，换个说法试试？',
              )
            }
          }
        }
      } finally {
        window.clearTimeout(timer)
        setSending(false)
      }
    },
    [sending, itineraryId, applyDraftPayload],
  )

  const applyDraft = useCallback(async () => {
    const current = msgsRef.current[activeActionIndex(msgsRef.current)]
    if (!current || typeof current.id !== 'number' || current.id <= 0 || !current.baseRevision) {
      setNotice('该草稿缺少确认信息，请重新生成')
      return false
    }
    setApplying(true)
    try {
      const detail = await applyPlans(itineraryId, current.id, current.baseRevision)
      onApplied(detail)
      await reload()
      return true
    } catch (error) {
      // 409 四道语义的文案由后端给，这里原样透传并触发行程对账
      setNotice(error instanceof Error ? error.message : '应用失败，请重试')
      onReconcile()
      return false
    } finally {
      setApplying(false)
    }
  }, [itineraryId, onApplied, onReconcile, reload])

  const applyHotel = useCallback(
    async (option: HotelOption, roomType: string, dayNos: number[]) => {
      const current = msgsRef.current[activeActionIndex(msgsRef.current)]
      setApplying(true)
      try {
        const detail = await applyHotelOption(
          itineraryId,
          { hotelName: option.hotelName || '', tier: option.tier || '', roomType, dayNos },
          typeof current?.id === 'number' && current.id > 0 ? current.id : null,
          current?.baseRevision || option.baseRevision || null,
        )
        onApplied(detail)
        await reload()
        return true
      } catch (error) {
        setNotice(error instanceof Error ? error.message : '酒店应用失败，请重试')
        onReconcile()
        return false
      } finally {
        setApplying(false)
      }
    },
    [itineraryId, onApplied, onReconcile, reload],
  )

  return { msgs, loaded, sending, applying, notice, setNotice, active, send, applyDraft, applyHotel, reload }
}
