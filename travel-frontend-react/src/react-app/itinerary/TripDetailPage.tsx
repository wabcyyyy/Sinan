import { hasIdentity, isDemoMode } from '../../demoMode'
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import {
  createPdfExport,
  createShare,
  exportDownloadUrl,
  getItineraryDetail,
  getItineraryWeather,
  isOfflineError,
  isUnauthorized,
  listMyItemFeedback,
  optimizeDay,
  regenerateDay as regenerateDayApi,
  revokeItemFeedback,
  setFavorite,
  submitItemFeedback,
  updateDay,
  waitForExport,
} from '../../api/sinan'
import { ReactApiError } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import type { DayPlan, ItineraryDetail } from '../../types/itinerary'
import { destinations, sampleItinerary } from '../data'
import { loginRedirect, navigate, routeId } from '../router'
import { safeAppLink } from '../../shared/map-link'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'
import { PublishItineraryPanel } from '../templates/PublishItineraryPanel'
import { fadeUp, SPRING_SNAPPY, staggerChildren } from '../shared/motion'
import { EmptyBlock, ErrorBlock, LoadingBlock, QualityNotice } from '../shared/States'
import { OpeningEvidence } from './OpeningEvidence'
import { DietaryEvidenceNotice, DraftOnlyBanner, ItemEvidence, TripMetrics } from './TripBadges'
import { detailGeneratingCopy } from './detailBanner'
import { ChatPanel } from './ChatPanel'
import { TripCarePanel, carePanelVisible } from './TripCarePanel'
import { feedbackIndex } from './itemFeedback'
import { ItemFeedbackControl } from './ItemFeedbackControl'
import { TripMapPanel } from './TripMapPanel'
import { amapLink, googleMapsLink, poiPhotoUrl } from './mapPins'
import type { MapPin } from './mapPins'

/** 时间线与类型徽章共用的图标映射（新类型先落到 pin，不裸奔）。 */
const ITEM_TYPE_ICONS: Record<string, 'camera' | 'utensils' | 'bed' | 'train' | 'ticket'> = {
  attraction: 'camera',
  food: 'utensils',
  hotel: 'bed',
  transport: 'train',
  activity: 'ticket',
}

/** 类型徽章文案（与 TripBoard 的 ITEM_TYPE_LABELS 同一套标签）。 */
const ITEM_TYPE_LABELS: Record<string, string> = {
  attraction: '游览',
  food: '用餐',
  hotel: '住宿',
  transport: '交通',
  activity: '活动',
}

function displayDate(value: string | null | undefined) {
  if (!value) return ''
  const date = new Date(`${value}T00:00:00`)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

/** 价格只说有数据的事：没有 cost 时不再补「特色美食/精选打卡」这类无出处的装饰词。 */
function getItemPriceTag(item: DayPlan['items'][0]): string | null {
  if (item.cost === 0) return '费用参考 ￥0 · 待核实'
  if (item.cost && item.cost > 0) return `参考 ￥${item.cost}${item.itemType === 'hotel' ? '/间晚' : '/人'}`
  return null
}

export function TripDetailPage({ path }: { path: string }) {
  const id = routeId(path, '/trips/') || '0'
  const [trip, setTrip] = useState<ItineraryDetail | null>(null)
  const [dayNo, setDayNo] = useState(1)
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const [authExpired, setAuthExpired] = useState(false)
  const [offline, setOffline] = useState(false)
  const [notice, setNotice] = useState('')
  const [shareUrl, setShareUrl] = useState('')
  const [downloadUrl, setDownloadUrl] = useState('')
  const [weather, setWeather] = useState<Contracts.WeatherVO | null>(null)
  const [editingTheme, setEditingTheme] = useState(false)
  const [themeDraft, setThemeDraft] = useState('')
  const [activePin, setActivePin] = useState<string | null>(null)
  // 条目点击展开（2026-10-12 反馈）：悬停不再联动地图——悬停就抢焦点/飞图会把页面拽走；
  // 点击才选中并展开地图出口，「在地图中定位」才主动把页面滚到地图
  const [expandedItem, setExpandedItem] = useState<string | null>(null)
  // 对话栏折叠（2026-09-30 评审拍板）：默认展开；收起后内容列占满剩余宽度。
  // 会话级状态不持久化——默认展开是产品主面。
  const [chatCollapsed, setChatCollapsed] = useState(false)
  // 条目对/错反馈（C3.5）：本人反馈索引 + 能力开关（GET 404 = addon 关，隐藏控件）
  const [feedbacks, setFeedbacks] = useState<Map<number, Contracts.FeedbackVO>>(new Map())
  const [feedbackOn, setFeedbackOn] = useState(false)
  // FEUX-2：分享/导出请求期锁按钮——双击会建 2 个 PDF 任务、2 个分享 token
  // 且后端单列覆盖写使旧分享链接静默失效（实测）；state 必须与其它 hooks
  // 一起放在 early return 之前（§3.1 同类教训）
  const [sharing, setSharing] = useState(false)
  const [exporting, setExporting] = useState(false)
  // FEUX-1：/trips/5 → /trips/999 切换时慢响应后到覆盖新响应——请求序号守卫，
  // 只有「最新一次 load」的结果才允许落地（列表页 TripsPage 同款）
  const loadSeqRef = useRef(0)
  // 出发前照看（个人 Agent V1）：对话草稿确认会消费 care 草稿——bump 此键让面板重取状态
  const [careRefreshTick, setCareRefreshTick] = useState(0)
  const [chatDraftRequest, setChatDraftRequest] = useState(0)

  const selectPin = (pin: MapPin) => {
    setDayNo(pin.dayNo)
    setActivePin(pin.key)
  }

  /** 查看后台草稿时先取最新聊天；由 ChatPanel 在载入完成后定位草稿。 */
  const openChatDraft = () => {
    setChatCollapsed(false)
    setChatDraftRequest((request) => request + 1)
  }

  /** 时间线条目点击：选中（地图飞到该点，不滚页面）+ 展开/收起地图出口 */
  const toggleItem = (itemKey: string) => {
    setActivePin(itemKey)
    setExpandedItem((current) => (current === itemKey ? null : itemKey))
  }

  /** 「在上方地图中定位」：用户明确要看地图时才把页面滚过去 */
  const locateOnMap = (itemKey: string) => {
    setActivePin(itemKey)
    document.querySelector('.trip-map-wrap')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const load = () => {
    const seq = ++loadSeqRef.current
    const isStale = () => seq !== loadSeqRef.current
    setLoading(true)
    setError('')
    setAuthExpired(false)
    setOffline(false)
    setWeather(null)
    setFeedbacks(new Map())
    setFeedbackOn(false)
    if (id === '0' || !hasIdentity()) {
      setTrip({ ...sampleItinerary, id: Number(id) || 0 })
      setOffline(true)
      setLoading(false)
      return
    }
    getItineraryDetail(id)
      .then((next) => { if (!isStale()) setTrip(next) })
      .catch((err: unknown) => {
        if (isStale()) return
        if (isUnauthorized(err)) {
          setAuthExpired(true)
          setError('登录已失效，请重新登录后继续查看这段旅程')
        } else if (isOfflineError(err)) {
          setTrip({ ...sampleItinerary, id: Number(id) || 0 })
          setOffline(true)
        } else {
          setError(err instanceof Error ? err.message : '行程读取失败')
        }
      })
      .finally(() => { if (!isStale()) setLoading(false) })
  }

  useEffect(() => { load() }, [id])

  /** 对话应用/冲突后的静默对账：不闪 loading，直接换上服务端最新详情。 */
  const refreshDetail = () => {
    if (offline || id === '0' || !hasIdentity()) return
    getItineraryDetail(id).then(setTrip).catch(() => {})
  }

  useEffect(() => {
    if (!trip || offline || id === '0' || !hasIdentity()) return
    let alive = true
    getItineraryWeather(id).then((value) => { if (alive && value.daily?.length) setWeather(value) }).catch(() => {})
    // 能力探针兼回显：200=开（拿到本人反馈），404=flag 关或非成员（隐藏，不暴露能力存在性）
    listMyItemFeedback(id)
      .then((list) => { if (alive) { setFeedbacks(feedbackIndex(list.feedbacks)); setFeedbackOn(true) } })
      .catch((err: unknown) => { if (alive && err instanceof ReactApiError && err.status === 404) setFeedbackOn(false) })
    return () => { alive = false }
    // status 进 deps：生成完成（1→2）后条目 ID 已全部换新，天气/回显要对着新详情重取
  }, [id, offline, trip?.id, trip?.status])

  // 生成中对账轮询（status=1）：后台逐日落库会软删旧条目再插新 ID，页面不刷新的话
  // 反馈等按条目 ID 的写操作必然 400；每 3s 静默拉详情，完成即停。
  useEffect(() => {
    if (!trip || offline || trip.status !== 1) return
    let alive = true
    const timer = setInterval(() => {
      getItineraryDetail(id).then((next) => { if (alive) setTrip(next) }).catch(() => {})
    }, 3000)
    return () => { alive = false; clearInterval(timer) }
  }, [id, offline, trip?.status])

  // toast 自动收敛：错误条曾挂 3 分钟+没人清；4s 到点自灭，手动关闭钮保留
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(''), 4000)
    return () => clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    if (trip) document.title = `${trip.city} · 司南 Sinan`
    return () => { document.title = '司南 Sinan' }
  }, [trip])

  const day = useMemo(() => trip?.dayList.find((item) => item.dayNo === dayNo) || trip?.dayList[0], [dayNo, trip])

  useEffect(() => {
    setThemeDraft(day?.theme || '')
    setEditingTheme(false)
  }, [day?.dayNo, day?.theme])

  // hooks 必须全部位于 early return 之前（React 规则）：本 effect 原先放在 loading/error
  // return 之后，/trips/5 → /trips/999 切换瞬间 hooks 数量变化会整页崩进根 ErrorBoundary
  useEffect(() => {
    if (!activePin) return
    const el = document.getElementById(`day-item-${activePin}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [activePin])

  if (loading) return <div className="page-content detail-state"><LoadingBlock label="正在打开这段旅程…" /></div>
  if (error || !trip) return <div className="page-content detail-state"><ErrorBlock message={error || '行程不存在'} onRetry={load} onLogin={authExpired ? () => navigate(loginRedirect()) : undefined} /></div>

  const handleWriteError = (err: unknown, fallback: string) => {
    if (isUnauthorized(err)) {
      setNotice('登录已失效，正在返回登录页…')
      navigate(loginRedirect())
      return
    }
    // DOMException 等异常 message 常为空串——空文案会让 toast 无声消失，统一落 fallback
    setNotice(err instanceof Error && err.message ? err.message : fallback)
  }

  const regenerateDay = async () => {
    if (offline || !day) { setNotice('这是离线示例，无法写回服务端'); return }
    setWorking(true)
    try {
      // 未交付天（PENDING/FAILED/空）走「重生成第 N 天」端点：真·LLM 重新生成且不另扣
      // 当日配额（部分交付自救）；已成功天保留 optimize 的确定性重排——那条路不烧 LLM。
      const delivered = day.generationStatus === 'SUCCEEDED'
        || (day.generationStatus == null && day.items.length > 0)
      const result = delivered
        ? await optimizeDay(trip.id, day.dayId)
        : await regenerateDayApi(trip.id, day.dayNo)
      setTrip(result)
      setNotice(delivered ? `第 ${day.dayNo} 天已重新排好` : `第 ${day.dayNo} 天已重新生成`)
    } catch (err) {
      handleWriteError(err, '重新生成失败')
    } finally { setWorking(false) }
  }

  const saveTheme = async () => {
    if (offline || !day) { setNotice('这是离线示例，无法写回服务端'); return }
    setWorking(true)
    try {
      const result = await updateDay(trip.id, day.dayId, themeDraft.trim())
      setTrip(result)
      setEditingTheme(false)
      setNotice('当天标题已保存')
    } catch (err) {
      handleWriteError(err, '保存失败')
    } finally { setWorking(false) }
  }

  const favorite = async () => {
    if (offline) { setNotice('示例行程不会写入账号'); return }
    try {
      const result = await setFavorite(trip.id, !trip.favorite)
      setTrip(result)
    } catch (err) { handleWriteError(err, '收藏失败') }
  }

  /** 反馈提交/撤销：父层 toast 后向控制组件 rethrow——选择器据失败保持打开供重试 */
  const applyFeedback = async (payload: Contracts.FeedbackCreate) => {
    if (offline || !trip) { setNotice('示例行程不会写入账号'); throw new Error('offline') }
    try {
      const vo = await submitItemFeedback(trip.id, payload)
      setFeedbacks((prev) => new Map(prev).set(vo.itemId, vo))
    } catch (err) {
      // 条目被重新生成后旧 ID 即失效（后端软删换新 ID）：提示后同步最新详情，
      // 选择器保持打开，用户对着新条目重选即可
      if (err instanceof ReactApiError && err.status === 400) {
        setNotice('这条安排刚被重新生成，已同步最新行程，请再选一次')
        refreshDetail()
      } else {
        handleWriteError(err, '反馈提交失败')
      }
      throw err
    }
  }

  const removeFeedback = async (itemId: number) => {
    if (offline || !trip) { setNotice('示例行程不会写入账号'); throw new Error('offline') }
    const drop = () => setFeedbacks((prev) => { const next = new Map(prev); next.delete(itemId); return next })
    try {
      await revokeItemFeedback(trip.id, itemId)
      drop()
    } catch (err) {
      // 已不存在（重复撤销）按成功收敛；其余交给统一错误提示
      if (err instanceof ReactApiError && err.status === 404) { drop(); return }
      handleWriteError(err, '反馈撤销失败')
      throw err
    }
  }

  // FEUX-2 的 busy 态见上方 hooks 区（sharing/exporting）

  const share = async () => {
    if (offline) { setNotice('登录并连接服务后才能创建分享链接'); return }
    if (sharing) return
    setSharing(true)
    try {
      const result = await createShare(trip.id)
      setShareUrl(result.shareUrl)
      // 剪贴板在自动化/非聚焦下可能拒绝且 message 为空串——单独兜住：链接已创建
      // 是事实，不能被剪贴板失败吞成「点分享无任何反馈」
      try {
        await navigator.clipboard?.writeText(result.shareUrl)
        setNotice('分享链接已复制')
      } catch {
        setNotice('分享链接已创建，见下方链接')
      }
    } catch (err) { handleWriteError(err, '分享失败') } finally { setSharing(false) }
  }

  const exportPdf = async () => {
    if (offline) { setNotice('示例行程暂不支持导出'); return }
    if (exporting) return
    setExporting(true)
    try {
      const task = await createPdfExport(trip.id)
      const done = task.status === 'DONE' ? task : await waitForExport(task.id)
      setDownloadUrl(done.downloadUrl || exportDownloadUrl(done.id))
      setNotice('PDF 已准备好，可以下载')
    } catch (err) { handleWriteError(err, '导出失败') } finally { setExporting(false) }
  }

  // 契约字段不直接进 href（P1-6）：过不了同源校验就退化为纯文本，不给用户一个任意跳转的链接
  const safeShareUrl = safeAppLink(shareUrl)
  const safeDownloadUrl = safeAppLink(downloadUrl)

  return <div className="detail-page">
    <div className="detail-topbar">
      <button className="back-link" type="button" onClick={() => navigate('/trips')}><Icon name="arrow" size={16} />我的行程</button>
      <div className="detail-actions">
        <button className={`icon-action${trip.favorite ? ' is-favorited' : ''}`} type="button" aria-label={trip.favorite ? '取消收藏' : '收藏'} aria-pressed={trip.favorite} onClick={favorite}><Icon name="heart" size={18} /></button>
        <button className="button button-secondary" type="button" disabled={sharing} onClick={share}><Icon name="share" size={16} />{sharing ? '创建中…' : '分享'}</button>
        <button className="button button-secondary" type="button" disabled={exporting} onClick={exportPdf}><Icon name="download" size={16} />{exporting ? '导出中…' : '导出'}</button>
      </div>
    </div>
    {offline && <div className="detail-offline"><span>示例状态</span> 这是本地预览，服务恢复后可打开真实行程。</div>}
    {trip.status === 1 && <div className="detail-generating" role="status"><span className="detail-generating-dot" aria-hidden="true" />{detailGeneratingCopy(trip.coreReady)}</div>}
    {(trip.genState === 'PARTIAL' || trip.genState === 'FAILED') && <div className="detail-offline" role="status"><strong>行程尚未完整交付</strong> · 已完成 {trip.dayList.filter((day) => day.generationStatus === 'SUCCEEDED').length}/{trip.days} 天。未完成日期的内容仅为草案，请切换到该日，点击「重新生成这一天」。</div>}
    <section className="detail-hero">
      <motion.div variants={staggerChildren(0.07)} initial="hidden" animate="show">
        <motion.span className="section-eyebrow" variants={fadeUp}>{trip.city} · {trip.days} 天 · {trip.persons} 人</motion.span>
        <motion.h1 variants={fadeUp}>{trip.title}</motion.h1>
        <motion.p variants={fadeUp}>{trip.tripTheme || trip.planNote}</motion.p>
        <motion.div className="detail-hero-facts" variants={fadeUp}>
        {/* 核实提示只留这一处：草案态由 DraftOnlyBanner 承担（含待核实条数），其余走 QualityNotice */}
        {trip.destinationStatus === 'draft_only'
          ? <DraftOnlyBanner status={trip.destinationStatus} pending={trip.pendingFactCount || 0} />
          : <QualityNotice status={trip.qualityStatus} pending={trip.pendingFactCount || 0} />}
        <DietaryEvidenceNotice trip={trip} />
        <TripMetrics
          trip={trip}
          dateText={trip.startDate ? displayDate(trip.startDate) : null}
          weatherDays={weather?.daily?.length || trip.days}
          hasWeather={Boolean(weather?.daily?.length)}
        />
        {weather?.daily?.length ? <div className="detail-weather" aria-label="天气参考"><span className="weather-label"><Icon name="sun" size={16} />天气参考</span>{weather.daily.slice(0, trip.days).map((item) => <span className="weather-day" key={item.date}><strong>{displayDate(item.date)}</strong><small>{item.text} · {item.tMin ?? '--'}–{item.tMax ?? '--'}℃</small></span>)}</div> : null}
        </motion.div>
      </motion.div>
      <motion.figure className="detail-cover" initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1], delay: 0.1 }}><SmartImg src={destinations.find((item) => item.city === trip.city)?.image || poiPhotoUrl(trip.city, trip.city)} alt={`${trip.city} 城市参考图`} ratio="16 / 10" eager /><figcaption><Icon name="pin" size={14} />{trip.city} · 城市印象</figcaption></motion.figure>
    </section>
    <div className={offline ? 'detail-workspace' : `detail-workspace has-chat${chatCollapsed ? ' is-chat-collapsed' : ''}`}>
      {!offline && (
        <ChatPanel
          itineraryId={trip.id}
          dayList={trip.dayList}
          onApplied={setTrip}
          onReconcile={refreshDetail}
          collapsed={chatCollapsed}
          onToggleCollapse={() => setChatCollapsed((value) => !value)}
          currentDayNo={dayNo}
          city={trip.city}
          onAfterApply={() => setCareRefreshTick((tick) => tick + 1)}
          draftRequest={chatDraftRequest}
        />
      )}
      <section className="day-content">
        {/* 出发前照看（个人 Agent V1）：只在真实模式挂载——离线样例与演示模式
            不渲染、不请求 care API（规格 §5 静态展示隔离）；不支持/未加载时面板自隐藏 */}
        {carePanelVisible({ offline, demo: isDemoMode() }) && (
          <TripCarePanel
            itineraryId={trip.id}
            dayList={trip.dayList}
            onViewDraft={openChatDraft}
            refreshKey={careRefreshTick}
          />
        )}
        {/* 天切换（2026-10-12 改版）：原独立侧栏列收进内容卡顶部的吸顶横条，工作区从三栏
            收成「对话 | 行程」两栏（与首页工作台同一心智）；选中底块是 layoutId 滑块 */}
        <nav className="day-tabs" aria-label="按天切换">{trip.dayList.map((item) => {
          const isActive = item.dayNo === day?.dayNo
          return <button key={item.dayNo} className={isActive ? 'day-tab active' : 'day-tab'} type="button" aria-current={isActive ? 'true' : undefined} onClick={() => setDayNo(item.dayNo)}>
            {isActive && <motion.span className="day-tab-bg" layoutId="detail-day-tab" transition={SPRING_SNAPPY} aria-hidden="true" />}
            <span>DAY {String(item.dayNo).padStart(2, '0')}</span>
            <strong>{item.theme || `第 ${item.dayNo} 天`}</strong>
            <small>{trip.status === 1 && !item.items.length ? '生成中…' : `${item.items.length} 个安排`}</small>
          </button>
        })}</nav>
        {day ? <>
        <TripMapPanel days={trip.dayList} activeKey={activePin} onSelect={selectPin} focusDayNo={dayNo} />
        <motion.div className="day-content-body" key={day.dayNo} variants={fadeUp} initial="hidden" animate="show">
        <div className="day-content-head"><div><span className="section-eyebrow">DAY {String(day.dayNo).padStart(2, '0')}</span>{editingTheme ? <div className="day-theme-editor"><input value={themeDraft} maxLength={80} onChange={(event) => setThemeDraft(event.target.value)} aria-label="当天标题" /><div><button className="button button-primary" type="button" disabled={working} onClick={saveTheme}>保存</button><button className="button button-secondary" type="button" disabled={working} onClick={() => { setThemeDraft(day.theme || ''); setEditingTheme(false) }}>取消</button></div></div> : <><h2>{day.theme || `第 ${day.dayNo} 天`}</h2><p>{day.note}</p></>}</div><div className="day-head-actions">{!offline && <button className="button button-secondary day-head-ai-btn" type="button" title="用对话微调当天的节奏、路线或美食" onClick={() => { setChatCollapsed(false); const input = document.querySelector('.chat-composer input, .chat-composer textarea') as HTMLInputElement | null; input?.focus() }}><Icon name="spark" size={15} />AI 编排此日</button>}{!editingTheme && <button className="button button-secondary" type="button" onClick={() => offline ? setNotice('示例行程不会写入账号') : setEditingTheme(true)}><Icon name="edit" size={16} />编辑标题</button>}<button className="button button-secondary" type="button" disabled={working || editingTheme} onClick={regenerateDay}><Icon name="refresh" size={16} />{working ? '正在整理…' : '重新生成这一天'}</button></div></div>
        <div className="day-items" key={day.dayNo}>{day.items.map((item, index) => {
          const itemKey = `${day.dayNo}:${index}`
          const isItemActive = activePin === itemKey
          const isExpanded = expandedItem === itemKey
          const hasCoord = item.latitude != null && item.longitude != null
          const nextItem = day.items[index + 1]
          // 实景图只认条目自身图片或服务端按名字解析的实景链——不再退回另一座
          // 城市的硬编码封面（那是把别处照片当本地点位展示，alt 还挂着本点名）。
          const itemImg = item.image || item.imageUrl || poiPhotoUrl(item.poiName, trip.city)
          return <div key={`${item.poiName}-${index}`} className="day-item-group">
            <article
              className={`day-item${isItemActive ? ' is-active' : ''}${isExpanded ? ' is-expanded' : ''}`}
              id={`day-item-${itemKey}`}
              style={{ '--stagger-i': index } as CSSProperties}
              tabIndex={0}
              aria-expanded={isExpanded}
              onClick={() => toggleItem(itemKey)}
              onKeyDown={(event) => {
                if (event.target !== event.currentTarget || (event.key !== 'Enter' && event.key !== ' ')) return
                event.preventDefault()
                toggleItem(itemKey)
              }}
            >
              <div className="day-item-time">{item.startTime || '--:--'}<span>{item.endTime || ''}</span></div>
              <div className="day-item-line"><i><Icon name={ITEM_TYPE_ICONS[item.itemType] || 'pin'} size={12} strokeWidth={2.2} /></i><span /></div>
              <div className="day-item-copy">
                <div className="item-heading">
                  <span className={`item-type item-type-${item.itemType}`}><Icon name={ITEM_TYPE_ICONS[item.itemType] || 'pin'} size={12} strokeWidth={2} />{ITEM_TYPE_LABELS[item.itemType] || '安排'}</span>
                  <h3>{item.poiName}</h3>
                </div>
                <p>{item.remark || item.whyThis || '为这一段旅程保留一点自由。'}</p>
                <div className="item-meta">
                  <span><Icon name="clock" size={14} />{item.durationMin ? `${item.durationMin} 分钟` : '时间可调整'}</span>
                  {getItemPriceTag(item) && <span><Icon name="ticket" size={14} />{getItemPriceTag(item)}</span>}
                  <ItemEvidence item={item} />
                  <span className="item-expand-hint"><Icon name="pin" size={12} />{isExpanded ? '收起' : '地图与导航'}<Icon name="chevron" size={12} className={isExpanded ? 'is-open' : ''} /></span>
                </div>
                <AnimatePresence initial={false}>
                  {isExpanded && (
                    <motion.div
                      className="item-expand"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <div className="item-expand-inner">
                        {hasCoord ? <>
                          <a className="item-map-link" href={amapLink({ latitude: item.latitude!, longitude: item.longitude!, poiName: item.poiName || '' })} target="_blank" rel="noreferrer"><Icon name="pin" size={13} />高德地图导航</a>
                          <a className="item-map-link" href={googleMapsLink({ latitude: item.latitude!, longitude: item.longitude!, poiName: item.poiName || '' })} target="_blank" rel="noreferrer"><Icon name="compass" size={13} />Google 地图</a>
                          <button className="item-map-link is-locate" type="button" onClick={() => locateOnMap(itemKey)}><Icon name="arrow" size={13} className="is-up" />在上方地图中定位</button>
                        </> : <span className="item-expand-note">这个地点暂时没有坐标，可以在地图 App 里搜索「{item.poiName}」核实位置。</span>}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
                {/* 反馈控件里的点击不冒泡成「展开/收起」 */}
                {feedbackOn && item.id ? <div onClick={(event) => event.stopPropagation()}><ItemFeedbackControl itemId={item.id} feedback={feedbacks.get(item.id)} onSet={applyFeedback} onRevoke={() => removeFeedback(item.id!)} /></div> : null}
              </div>
              <div className="day-item-media" aria-hidden="true">
                <SmartImg src={itemImg} alt={item.poiName || '地点实景'} ratio="1 / 1" />
              </div>
              {isExpanded && <OpeningEvidence item={item} />}
            </article>
            {nextItem && (
              <div className="day-transit-row" aria-label="前往下一站建议">
                <div className="transit-track"><i /><span /></div>
                <div className="transit-pill">
                  <Icon name="train" size={11} />
                  <span>前往下站 · 交通时间以地图导航为准</span>
                </div>
              </div>
            )}
          </div>
        })}</div>
        {(day.generationStatus === 'PENDING' || day.generationStatus === 'FAILED') && day.items.length > 0 && (
          <div className="day-note" role={trip.status === 1 ? 'status' : 'note'}>
            <Icon name="alert" size={17} />
            <div>
              <strong>{trip.status === 1 ? '这一天还在校对中' : '这一天的安排未通过最终校验'}</strong>
              <p>{trip.status === 1 ? '终检或重排完成后会自动更新，当前内容仅供参考。' : '内容偏少或存在冲突，可以点「重新生成这一天」重新补齐（不另扣今日 AI 次数），或让 AI 帮你补充。'}</p>
            </div>
          </div>
        )}
        {trip.status !== 1 && !day.items.length && (
          <div className="day-note" role="note">
            <Icon name="alert" size={17} />
            <div>
              <strong>这一天还没有生成成功</strong>
              <p>点上方「重新生成这一天」即可补齐（不另扣今日 AI 次数），或让 AI 帮你补充。</p>
            </div>
          </div>
        )}
        {trip.status === 1 && !day.items.length && (
          <div className="day-generating-skeleton" role="status" aria-label="正在生成这一天的安排">
            <div className="day-generating-notice">
              <span className="loading-orbit" aria-hidden="true" />
              <span>司南正在细排第 {day.dayNo} 天的节奏、路线与推荐，完成后自动更新…</span>
            </div>
            <div className="day-skeleton-items" aria-hidden="true">
              {[1, 2, 3].map((k) => (
                <div key={k} className="day-skeleton-card" style={{ '--stagger-i': k } as CSSProperties}>
                  <div className="skeleton-time-col">
                    <span className="skeleton-bar skeleton-bar-time" />
                  </div>
                  <div className="skeleton-line-col">
                    <i /><span />
                  </div>
                  <div className="skeleton-body-col">
                    <div className="skeleton-heading-row">
                      <span className="skeleton-bar skeleton-bar-pill" />
                      <span className="skeleton-bar skeleton-bar-title" />
                    </div>
                    <span className="skeleton-bar skeleton-bar-desc" />
                    <span className="skeleton-bar skeleton-bar-meta" />
                  </div>
                  <div className="skeleton-media-col">
                    <span className="skeleton-bar skeleton-bar-thumb" />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        {day.practicalNotes?.length ? <div className="day-note is-tips"><Icon name="compass" size={17} /><div><strong>出发前核实清单</strong><p>以下为规划建议；开放、预约、营业与班次信息请以官方当日公告为准。</p>{day.practicalNotes.map((note) => <p key={note}>{note}</p>)}</div></div> : null}
        </motion.div>
      </> : <EmptyBlock title="这一天还没有安排" description="可以先切换到其他天，或重新生成当天内容。" />}</section>
    </div>
    <PublishItineraryPanel key={trip.id} trip={trip} />
    {shareUrl && <div className="share-result"><span>分享链接</span>{safeShareUrl ? <a href={safeShareUrl} target="_blank" rel="noreferrer">{safeShareUrl}</a> : <span>{shareUrl}</span>}<button type="button" onClick={() => setShareUrl('')} aria-label="关闭分享链接"><Icon name="close" size={15} /></button></div>}
    {downloadUrl && <div className="download-result"><span>PDF 已就绪</span>{safeDownloadUrl ? <a className="button button-primary" href={safeDownloadUrl} download>下载 PDF<Icon name="download" size={15} /></a> : <span>下载 PDF</span>}<button type="button" onClick={() => setDownloadUrl('')} aria-label="关闭下载提示"><Icon name="close" size={15} /></button></div>}
    {notice && <div className="toast-note" role="status">{notice}<button type="button" onClick={() => setNotice('')} aria-label="关闭提示"><Icon name="close" size={14} /></button></div>}
  </div>
}

