import { useEffect, useRef, useState } from 'react'
import type { ItineraryChatMessage } from '../../types/chat'
import type { DayPlan, HotelOption, ItineraryDetail } from '../../types/itinerary'
import { interpretImageIntent, isUnauthorized, liveHotelQuotes } from '../../api/sinan'
import { ChatComposer, composeDraft, imageFeedbackText } from '../shared/ChatComposer'
import { Icon } from '../shared/Icon'
import { renderChatMarkdown } from './chatMarkdown'
import { activeActionIndex, describeStructuredChange, diffBadge, hotelDefaultSelection, pendingActionSummary, structuredDraftChanges, unverifiedNames } from './chatDraft'
import { getDayQuickPrompts, getPromptSceneGroups } from './chatPrompts'
import { describeLiveQuotesError, liveQuotesCaption, quoteRowMeta } from './hotelLiveQuotes'
import type { LiveQuotesState } from './hotelLiveQuotes'
import { useTripChat } from './useTripChat'

/** 对话编排主面（CH3）：对话在左、行程保持可见；草稿卡/确认卡长在对话流内。
 * 选择型 UI（酒店候选）保留结构化点选，不做全屏对话。
 * 折叠入口（2026-09-30 评审拍板）：默认展开；收起成窄条，由调用方持有状态并
 * 同步改 grid 列宽（收起时内容列占满剩余宽度）。不传 onToggleCollapse 则无入口。 */
export function ChatPanel({
  itineraryId,
  dayList,
  onApplied,
  onReconcile,
  collapsed = false,
  onToggleCollapse,
  currentDayNo = 1,
  city,
}: {
  itineraryId: number
  dayList: DayPlan[]
  onApplied: (detail: ItineraryDetail) => void
  onReconcile: () => void
  collapsed?: boolean
  onToggleCollapse?: () => void
  currentDayNo?: number
  city?: string
}) {
  const chat = useTripChat(itineraryId, onApplied, onReconcile)
  const [draft, setDraft] = useState('')
  const [imageBusy, setImageBusy] = useState(false)
  const logRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [chat.msgs.length, chat.sending])

  const submit = () => {
    const text = draft
    setDraft('')
    void chat.send(text)
  }

  const handleQuickSend = (text: string) => {
    if (chat.sending) return
    setDraft('')
    void chat.send(text)
  }

  // 图片→一句话：建议消息回填草稿交用户编辑后照常 send（不自动发送），
  // 错误走 ChatPanel 既有 notice 通道
  const handleImage = async (file: File) => {
    chat.setNotice('')
    setImageBusy(true)
    try {
      const res = await interpretImageIntent(file)
      setDraft((current) => composeDraft(current, res.suggestedMessage, 2000))
    } catch (err) {
      chat.setNotice(isUnauthorized(err) ? '登录状态已过期，重新登录后就能识别图片。' : imageFeedbackText(err))
    } finally {
      setImageBusy(false)
    }
  }

  const activeIdx = activeActionIndex(chat.msgs)
  const quickPrompts = getDayQuickPrompts(currentDayNo, city)
  const sceneGroups = getPromptSceneGroups(currentDayNo, city)

  return <section className={collapsed ? 'chat-panel is-collapsed' : 'chat-panel'} aria-label="行程对话编排">
    <div className="chat-panel-head">
      <span className="chat-symbol"><Icon name="compass" size={20} /></span>
      {!collapsed && <div><h2>对话编排</h2><p>想改什么直接说，确认后才会落图。</p></div>}
      {onToggleCollapse && (
        <button className="chat-collapse" type="button" aria-label={collapsed ? '展开对话' : '收起对话'} aria-expanded={!collapsed} onClick={onToggleCollapse}>
          <Icon name={collapsed ? 'chevron' : 'close'} size={16} />
        </button>
      )}
    </div>
    {!collapsed && <>
    <div className="chat-log" role="log" aria-live="polite" ref={logRef}>
      {!chat.loaded && <div className="chat-empty">正在载入对话…</div>}
      {chat.loaded && !chat.msgs.length && (
        <div className="chat-empty">
          <div className="chat-empty-lead">
            <span>还没有对话。对行程说一句话，立即智能微调：</span>
          </div>
          <div className="chat-suggest">
            {['第二天加个博物馆', '换个舒服点的酒店', '把第二天安排松一点'].map((hint) => (
              <button key={hint} type="button" onClick={() => handleQuickSend(hint)}>{hint}</button>
            ))}
          </div>
          <div className="chat-scenes-guide">
            {sceneGroups.map((group) => (
              <div key={group.sceneTitle} className="chat-scene-group">
                <span className="chat-scene-title">{group.sceneTitle}</span>
                <div className="chat-scene-chips">
                  {group.prompts.map((p) => (
                    <button key={p} type="button" className="chat-scene-chip" onClick={() => handleQuickSend(p)}>
                      {p}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {chat.msgs.map((msg, index) => {
        const isUser = msg.role === 'user'
        const isDraft = index === activeIdx
        return (
          <div key={msg.id ?? `idx-${index}`} className={isUser ? 'chat-msg is-user' : 'chat-msg is-ai'}>
            <div className="chat-bubble">{isUser
              ? msg.content
              // AI 文案带 ###/** 标记，走最小 MD 渲染；用户输入一律原文。
              // 流式回合里 AI 消息先占位，正文到达前显示跳点思考态。
              : (msg.content ? renderChatMarkdown(msg.content) : <span className="chat-thinking" aria-label="司南正在想"><i /><i /><i /></span>)}</div>
            {isDraft && (
              <DraftCard
                msg={msg}
                itineraryId={itineraryId}
                dayList={dayList}
                applying={chat.applying}
                onApply={() => void chat.applyDraft()}
                onApplyHotel={(option, roomType, dayNos) => void chat.applyHotel(option, roomType, dayNos)}
              />
            )}
          </div>
        )
      })}
    </div>
    {chat.notice && (
      <p className="chat-notice" role="alert">
        {chat.notice}
        <button type="button" aria-label="关闭提示" onClick={() => chat.setNotice('')}><Icon name="close" size={13} /></button>
      </p>
    )}

    {/* 针对当前查看天数的智能微调指令条（对话改一切） */}
    <div className="chat-quick-bar" aria-label="智能快捷微调">
      <div className="chat-quick-label">
        <Icon name="spark" size={12} />
        <span>第 {currentDayNo} 天微调：</span>
      </div>
      <div className="chat-quick-scroll">
        {quickPrompts.map((item) => (
          <button
            key={item.id}
            type="button"
            className="chat-quick-chip"
            title={item.prompt}
            disabled={chat.sending}
            onClick={() => handleQuickSend(item.prompt)}
          >
            {item.label}
          </button>
        ))}
      </div>
    </div>

    <ChatComposer
      className="chat-composer"
      value={draft}
      onChange={setDraft}
      onSend={submit}
      placeholder="例如：第二天加个博物馆、顺路重排"
      ariaLabel="对行程说话"
      maxLength={2000}
      disabled={chat.sending}
      onImage={handleImage}
      imageBusy={imageBusy}
      onNotice={chat.setNotice}
    />
    </>}
  </section>
}

/** 草稿卡/确认卡：挂在带草稿的 AI 消息下方（导出仅供单测静态渲染）。 */
export function DraftCard({
  msg,
  itineraryId,
  dayList,
  applying,
  onApply,
  onApplyHotel,
}: {
  msg: ItineraryChatMessage
  itineraryId: number
  dayList: DayPlan[]
  applying: boolean
  onApply: () => void
  onApplyHotel: (option: HotelOption, roomType: string, dayNos: number[]) => void
}) {
  const changes = msg.plans?.length ? structuredDraftChanges(dayList, msg.plans) : []
  const unverified = msg.plans?.length ? unverifiedNames(dayList, msg.plans) : []
  return <div className="chat-draft">
    {msg.requiresConfirmation && msg.pendingAction && (
      <div className="chat-confirm" role="status">
        <strong>需要你确认</strong>
        <span>{pendingActionSummary(msg.pendingAction)}，从下方候选中点选即可确认生效。</span>
      </div>
    )}
    {changes.length > 0 && (
      <div className="chat-diff-box">
        <div className="chat-diff-head">
          <Icon name="spark" size={13} />
          <strong>AI 编排调整建议</strong>
        </div>
        <ul className="chat-diff">
          {changes.map((change, idx) => {
            const badge = diffBadge(change)
            return (
              <li key={idx} className={`chat-diff-line ${badge ? badge.className : ''}`}>
                {badge && <span className={`diff-badge ${badge.className}`}>{badge.text}</span>}
                <span className="diff-text">{describeStructuredChange(change)}</span>
              </li>
            )
          })}
        </ul>
      </div>
    )}
    {unverified.length > 0 && <p className="chat-unverified">这些点位 AI 没拿到坐标，出发前请自行核实：{unverified.join('、')}</p>}
    {msg.hotelOptions?.length ? (
      <HotelChooser options={msg.hotelOptions} tripId={itineraryId} tripDays={dayList.length} applying={applying} onApply={onApplyHotel} />
    ) : null}
    {msg.plans?.length ? (
      <button className="button button-primary chat-apply" type="button" disabled={applying} onClick={onApply}>
        {applying ? '正在应用…' : '应用到行程'}
      </button>
    ) : null}
  </div>
}

function HotelChooser({
  options,
  tripId,
  tripDays,
  applying,
  onApply,
}: {
  options: HotelOption[]
  tripId: number
  tripDays: number
  applying: boolean
  onApply: (option: HotelOption, roomType: string, dayNos: number[]) => void
}) {
  const [idx, setIdx] = useState(0)
  const option = options[Math.min(idx, options.length - 1)]
  const [selection, setSelection] = useState(() => hotelDefaultSelection(option, tripDays))
  useEffect(() => {
    setSelection(hotelDefaultSelection(option, tripDays))
  }, [option, tripDays])
  // 实时价（L16）是城市+日期窗级的对照信息，不是单候选的属性——查询一次，
  // 整个选择器共享；服务端按用户分钟窗限速，前端只做 loading 去抖。
  const [live, setLive] = useState<LiveQuotesState>({ status: 'idle' })
  const fetchLive = () => {
    if (live.status === 'loading') return
    setLive({ status: 'loading' })
    liveHotelQuotes(tripId).then(
      (data) => setLive({ status: 'ready', quotes: data }),
      (err: unknown) => setLive({ status: 'error', message: describeLiveQuotesError(err) }),
    )
  }

  return <div className="chat-hotels">
    {options.length > 1 && (
      <div className="chat-hotel-tabs" role="tablist" aria-label="酒店候选">
        {options.map((item, index) => (
          <button key={item.id ?? index} type="button" role="tab" aria-selected={index === idx} className={index === idx ? 'active' : ''} onClick={() => setIdx(index)}>
            {item.hotelName || `候选 ${index + 1}`}
          </button>
        ))}
      </div>
    )}
    <div className="chat-hotel-body">
      <div className="chat-hotel-line"><strong>{option.hotelName}</strong><span>{option.tier} · ￥{option.totalPrice ?? '--'}{option.nights ? ` / ${option.nights} 晚` : ''}{option.withinBudget === false ? ' · 超预算' : ''}{option.searchLink && <> · <a className="chat-hotel-verify" href={option.searchLink} target="_blank" rel="noreferrer" aria-label={`在地图核实 ${option.hotelName}`}>地图核实</a></>}</span></div>
      {option.roomTypes?.length ? (
        <label className="chat-hotel-room">房型
          <select value={selection.roomType} onChange={(event) => setSelection((current) => ({ ...current, roomType: event.target.value }))}>
            {option.roomTypes.map((room) => (
              <option key={room.roomName} value={room.roomName}>
                {room.roomName}{room.breakfast ? ' · 含早' : ''}（￥{room.totalPrice ?? room.nightlyPrice ?? '--'}）
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <div className="chat-hotel-foot">
        <span>入住晚次：第 {selection.dayNos.join('、第 ') || '--'} 晚</span>
        <button className="button button-primary" type="button" disabled={applying || !selection.roomType} onClick={() => onApply(option, selection.roomType, selection.dayNos)}>
          {applying ? '正在应用…' : '确认入住'}
        </button>
      </div>
    </div>
    <div className="chat-hotel-live">
      <div className="chat-hotel-live-head">
        <span>候选价是司南的估算，实付以酒店为准。</span>
        <button className="text-action" type="button" disabled={live.status === 'loading'} onClick={fetchLive}>
          {live.status === 'loading' ? '正在查…' : live.status === 'ready' ? '再查一次' : '查实时价'}
        </button>
      </div>
      {live.status === 'ready' && live.quotes && (live.quotes.hotelQuotes.length ? (
        <div className="chat-hotel-live-rows">
          <p className="chat-hotel-live-cap">{liveQuotesCaption(live.quotes)}</p>
          {live.quotes.hotelQuotes.map((row) => (
            <div key={row.name} className="chat-hotel-live-row">
              <strong>{row.name}</strong>
              <span>￥{row.nightlyPrice}/晚{row.totalPrice != null ? ` · 共￥${row.totalPrice}` : ''}{quoteRowMeta(row) ? ` · ${quoteRowMeta(row)}` : ''}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="chat-hotel-live-empty">{live.quotes.reason || '没查到实时报价。'}</p>
      ))}
      {live.status === 'error' && <p className="chat-hotel-live-empty" role="alert">{live.message}</p>}
    </div>
  </div>
}
