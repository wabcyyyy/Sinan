import { useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import type { GenerateInput } from '../../api/sinan'
import { ChatIntake } from './ChatIntake'
import { HomeShowcase } from './HomeShowcase'
import { TripPanel } from './TripPanel'
import { saveIntakeSession } from './intakeHistory'
import type { IntakeSessionRecord } from './intakeHistory'
import { FLIP_DURATION_MS, useFlip } from './useFlip'
import { useHomePlanning } from './useHomePlanning'
import { useIntakeChat } from './useIntakeChat'

/** 首页相位（PLAN 2026-10-02 §2.1）：idle 居中单卡 ↔ active 双栏工作台。 */
export type HomePhase = 'idle' | 'active'

/** 创建工作台：idle 一张居中对话卡；active = planning.status!=='idle'（含刷新续看
 * sinan-intake-generation 恢复）或有对话历史（sessionStorage 恢复同理）——两条红线
 * 都要算进派生，否则刷新续看后进度无处显示（PLAN §7.3）。进 active 后不回 idle，
 * 唯一出口是「重新说」重置会话。双栏切换用 useFlip 回放对话卡位移，右栏纯 CSS
 * 淡入（延迟 = FLIP 时长 60%，经 --flip-duration 内联变量同源）。
 * 生成完成后留在首页看预览（拍板①）：done 态由右栏 TripBoard 呈现，
 * 详情页经预览板 CTA（/trips/:id）进入，不再自动跳转。idle 态在一屏余量之后
 * 挂轻展示区 HomeShowcase（PLAN 2026-10-03 §2.2），active 双栏不渲染。 */
export function HomeStudio({ query }: { query: URLSearchParams }) {
  const planning = useHomePlanning()
  const chat = useIntakeChat()
  const [resetToIdle, setResetToIdle] = useState(false)

  // 「重新说」重置会话 → 强制回 idle 居中：chat.reset 清对话与槽位，planning.reset
  // 清生成态与 generationId（done 态保留的 id 唯一清空点，见 useHomePlanning.reset；
  // 不清的话重置后的刷新会被 resume 拉回旧行程）。resetToIdle 锁把重置后的相位钉在
  // idle，解锁信号是新消息进会话（messages.length>1），锁只活一次。
  const derivedPhase: HomePhase = planning.status !== 'idle' || chat.messages.length > 1 ? 'active' : 'idle'
  const phase: HomePhase = resetToIdle ? 'idle' : derivedPhase
  const active = phase === 'active'
  useEffect(() => {
    if (resetToIdle && chat.messages.length > 1) setResetToIdle(false)
  }, [resetToIdle, chat.messages.length])

  const chatCardRef = useFlip<HTMLDivElement>(active)

  // 变换后焦点保持在输入框：composer 不重挂、焦点天然保留；这里兜底抢回。
  // 初始挂载即 active（刷新续看）不抢——没有用户手势就夺焦会弹移动端键盘。
  const wasActiveRef = useRef(true)
  useEffect(() => {
    const was = wasActiveRef.current
    wasActiveRef.current = active
    if (!active || was) return
    const card = chatCardRef.current
    if (card && !card.contains(document.activeElement)) {
      card.querySelector<HTMLInputElement>('input[aria-label="说说你的旅行想法"]')?.focus()
    }
  }, [active, chatCardRef])

  const startGeneration = (input: GenerateInput) => void planning.submit(input)
  // ChatIntake 内 IntakeConfirm 的 onReset 换成这个包装：自动暂存会话草稿，
  // 会话重置（对话+生成态一并归零）+ 回 idle 居中
  const handleChatReset = () => {
    if (chat.messages.some((m) => m.role === 'user') || chat.slots.city) {
      saveIntakeSession({
        slots: chat.slots,
        messages: chat.messages,
        firstMessage: chat.firstMessage,
        generationId: planning.draft?.id ? String(planning.draft.id) : null,
      })
    }
    chat.reset()
    planning.reset()
    setResetToIdle(true)
  }

  const handleResumeSession = (session: IntakeSessionRecord) => {
    chat.restoreSession(session)
    setResetToIdle(false)
  }

  useEffect(() => {
    const onResetHome = () => {
      handleChatReset()
    }
    window.addEventListener('sinan:reset-home', onResetHome)
    return () => window.removeEventListener('sinan:reset-home', onResetHome)
  }, [chat, planning])

  return <>
    <div
      className={active ? 'home-studio is-active' : 'home-studio is-idle'}
      id="planning"
      style={{ '--flip-duration': `${FLIP_DURATION_MS}ms` } as CSSProperties}
    >
      {!active && (
        <div className="home-ambient-aurora" aria-hidden="true">
          <div className="aurora-blob aurora-coral" />
          <div className="aurora-blob aurora-azure" />
          <div className="aurora-blob aurora-warm" />
        </div>
      )}
      <div className="home-studio-chat" ref={chatCardRef}>
        <ChatIntake
          query={query}
          chat={{ ...chat, reset: handleChatReset }}
          disabled={planning.busy}
          variant={active ? 'dock' : 'centered'}
          onResumeSession={handleResumeSession}
        />
      </div>
      {active && <div className="home-studio-panel"><TripPanel planning={planning} chat={{ ...chat, reset: handleChatReset }} onStart={startGeneration} /></div>}
    </div>
    {/* idle 轻展示区（PLAN 2026-10-03 §2.2）：作为 studio 的兄弟块排在一屏余量之后，
       天然落在折叠线以下、不挤占对话卡的居中（FLIP 量的是卡带自身，几何不受影响）；
       active 双栏不渲染——工作台不掺导流内容 */}
    {!active && <HomeShowcase />}
  </>
}
