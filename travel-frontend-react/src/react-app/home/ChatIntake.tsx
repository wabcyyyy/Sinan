import { useEffect, useRef, useState } from 'react'
import { interpretImageIntent, isUnauthorized } from '../../api/sinan'
import { inspirationTemplates } from '../data'
import { loginRedirect, navigate } from '../router'
import { BrandMark, HeroLogo } from '../shared/Brand'
import {
  ChatComposer,
  composeDraft,
  imageFeedbackText,
  TRAVEL_PROMPT_SUGGESTIONS,
} from '../shared/ChatComposer'
import { Icon } from '../shared/Icon'
import {
  clearAllIntakeSessions,
  deleteIntakeSession,
  formatSessionTime,
  listIntakeSessions,
} from './intakeHistory'
import type { IntakeSessionRecord } from './intakeHistory'
import { IntakeSessionsModal } from './IntakeSessionsModal'
import { GREETING, seedFromQuery } from './intakeSlots'
import type { IntakeChat } from './useIntakeChat'

/** 冷启动开场模板：精选 3 条最具代表性的起点（PLAN 2026-10-02 §2.2），
 * 单排一字展开，保持视觉克制从容不换行。点击只预填草稿不自动发送。 */
const GREETING_TEMPLATES = inspirationTemplates.slice(0, 3)

/** 对话式创建壳：clarify 多轮收集（chips 点选即答），ready 后就地确认再开工。
 * 集齐 city/days/persons 前不出「开始规划」——不猜测、不零追问直出。
 * 会话状态由宿主持有（HomeStudio 要用 messages 派生 idle/active）经 chat 下发；
 * variant 给两种宽度变体：centered=idle 居中卡，dock=active 左栏（卡3 栏内复用）。 */
export function ChatIntake({
  query,
  chat,
  disabled,
  variant,
  onResumeSession,
}: {
  query: URLSearchParams
  chat: IntakeChat
  disabled?: boolean
  variant?: 'centered' | 'dock'
  onResumeSession?: (session: IntakeSessionRecord) => void
}) {
  const [draft, setDraft] = useState(() => seedFromQuery(query))
  const [imageBusy, setImageBusy] = useState(false)
  const [imageNotice, setImageNotice] = useState('')
  const [imageNeedsLogin, setImageNeedsLogin] = useState(false)
  const logRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [chat.messages.length, chat.sending])

  const locked = disabled || chat.sending
  const last = chat.messages[chat.messages.length - 1]
  const chips = !chat.ready && !locked && last?.role === 'assistant' && last.options?.length ? last.options : null
  // 纯 greeting 的冷启动会话：greeting 下挂模板 chips 导流，开聊即收起
  const fresh = chat.messages.length === 1 && chat.messages[0].id === GREETING.id

  const submit = () => {
    const text = draft
    setImageNotice('')
    setImageNeedsLogin(false)
    setDraft('')
    void chat.send(text)
  }

  // 图片→一句话：识别结果回填草稿交用户编辑后照常 send（不自动发送）；
  // 401 沿用 needsLogin 通道给登录入口，其余错误走 intake 既有提示位
  const handleImage = async (file: File) => {
    setImageNotice('')
    setImageNeedsLogin(false)
    setImageBusy(true)
    try {
      const res = await interpretImageIntent(file)
      setDraft((current) => composeDraft(current, res.suggestedMessage, 800))
    } catch (err) {
      if (isUnauthorized(err)) {
        setImageNeedsLogin(true)
        setImageNotice('登录后就能识别图片，你已填的想法会保留。')
        return
      }
      setImageNotice(imageFeedbackText(err))
    } finally {
      setImageBusy(false)
    }
  }

  const [sessions, setSessions] = useState<IntakeSessionRecord[]>(() => listIntakeSessions())
  const [showSessionsModal, setShowSessionsModal] = useState(false)
  const refreshSessions = () => setSessions(listIntakeSessions())

  return (
    <>
      <div className={variant ? `intake-wrapper intake-wrapper-${variant}` : 'intake-wrapper intake-wrapper-centered'}>
        {/* 对话框外部 Hero 区域：HeroLogo 品牌大徽标 + 大标题 + 诗意副标 */}
        {variant !== 'dock' && (
          <header className="intake-hero">
            <div className="intake-hero-brand">
              <HeroLogo />
              <span className="intake-hero-badge">✦ AI 目的地探索与智能编排</span>
            </div>
            <h1 className="intake-hero-title">去哪里？说一句话，行程就有了</h1>
            <p className="intake-hero-subtitle">信息不够司南会问你，不会瞎猜 · 随时调整路线与偏好</p>
          </header>
        )}

        <div className={variant ? `intake intake-${variant}` : 'intake intake-centered'} aria-label="对话式行程创建">
          {variant === 'dock' && (
            <div className="intake-head intake-dock-head">
              <div className="intake-head-brand">
                <span className="intake-symbol"><BrandMark /></span>
                <div><h1>智能编排对话</h1><p>随时输入你想调整的想法</p></div>
              </div>
              <button
                className="text-action intake-reset-btn"
                type="button"
                onClick={chat.reset}
                title="退出对话，返回首页"
              >
                <Icon name="arrow" size={12} className="intake-back-icon" />
                返回首页
              </button>
            </div>
          )}

          {/* 历史对话草稿恢复条（对话管理）：避免中途退出导致数据丢失 */}
          {fresh && variant === 'centered' && sessions.length > 0 && (
            <div className="intake-resume-bar" aria-label="未完成规划草稿提示">
              <div className="resume-bar-lead">
                <span className="resume-bar-icon"><Icon name="clock" size={14} /></span>
                <div className="resume-bar-text">
                  <span className="resume-bar-label">未完成的规划：</span>
                  <strong className="resume-bar-title">{sessions[0].title}</strong>
                  <span className="resume-bar-time">{formatSessionTime(sessions[0].updatedAt)}</span>
                </div>
              </div>
              <div className="resume-bar-actions">
                <button
                  type="button"
                  className="button button-primary resume-bar-cta"
                  onClick={() => onResumeSession?.(sessions[0])}
                >
                  继续上次对话
                </button>
                <button
                  type="button"
                  className="text-action resume-bar-manage"
                  onClick={() => setShowSessionsModal(true)}
                >
                  对话记录 ({sessions.length})
                </button>
              </div>
            </div>
          )}

          <div className="intake-log" role="log" aria-live="polite" ref={logRef}>
            {chat.messages.map((msg) => (
              <div key={msg.id} className={`intake-msg is-${msg.role}`}>{msg.text}</div>
            ))}
            {/* dock 模式下（左侧分栏）保留栏内备用 chips */}
            {fresh && variant === 'dock' && (
              <div className="intake-chips" aria-label="试试这些开场">
                {GREETING_TEMPLATES.map((item) => (
                  <button key={item.id} type="button" title={item.description} onClick={() => setDraft(item.intent.slice(0, 800))}>{item.title}</button>
                ))}
              </div>
            )}
            {chat.sending && <div className="intake-msg is-assistant is-typing"><span className="chat-thinking" aria-label="司南正在想"><i /><i /><i /></span></div>}
            {chips && <div className="intake-chips">
              {chips.map((option) => <button key={option} type="button" onClick={() => void chat.send(option)}>{option}</button>)}
            </div>}
          </div>
          {chat.error && <p className="planning-retry-hint" role="alert">{chat.error}</p>}
          {imageNotice && <p className="planning-retry-hint" role="alert">{imageNotice}</p>}
          {(chat.needsLogin || imageNeedsLogin) && (
            <button className="button button-primary" type="button" onClick={() => navigate(loginRedirect())}>
              登录并继续<Icon name="arrow" size={16} />
            </button>
          )}
          <ChatComposer
            className="intake-composer"
            value={draft}
            onChange={setDraft}
            onSend={submit}
            placeholder="例如：国庆想去成都玩 4 天，两个人，预算 3000"
            placeholderList={TRAVEL_PROMPT_SUGGESTIONS}
            ariaLabel="说说你的旅行想法"
            maxLength={800}
            disabled={locked}
            onImage={handleImage}
            imageBusy={imageBusy}
            onNotice={setImageNotice}
          />
        </div>

        {/* 灵感推荐卡片：移至对话框外部下方，呈现精美药丸微卡片 */}
        {fresh && variant !== 'dock' && (
          <div className="intake-suggestions-bar">
            <div className="intake-chips" aria-label="试试这些开场">
              <span className="suggestions-lead"><Icon name="spark" size={13} />灵感推荐：</span>
              {GREETING_TEMPLATES.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="intake-chip-card"
                  title={item.description}
                  onClick={() => setDraft(item.intent.slice(0, 800))}
                >
                  <span className="chip-badge">{item.city} · {item.days}天</span>
                  <span className="chip-title">{item.title}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <IntakeSessionsModal
        sessions={sessions}
        open={showSessionsModal}
        onClose={() => setShowSessionsModal(false)}
        onResume={(session) => {
          setShowSessionsModal(false)
          onResumeSession?.(session)
        }}
        onDelete={(id) => {
          deleteIntakeSession(id)
          refreshSessions()
        }}
        onClearAll={() => {
          clearAllIntakeSessions()
          refreshSessions()
          setShowSessionsModal(false)
        }}
      />
    </>
  )
}
