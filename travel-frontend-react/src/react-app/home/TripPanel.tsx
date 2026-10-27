import { motion } from 'motion/react'
import type { GenerateInput } from '../../api/sinan'
import type { DayPlan, ItineraryDetail } from '../../types/itinerary'
import { loginRedirect, navigate } from '../router'
import { Icon } from '../shared/Icon'
import { chipPop, EASE_OUT, fadeUp, SPRING_LAYOUT, SPRING_POP, SPRING_SNAPPY, staggerChildren } from '../shared/motion'
import { candidatesForDay } from './itemPreviews'
import type { ItemPreviewCandidate } from './itemPreviews'
import { toGenerateInput } from './intakeSlots'
import { IntakeConfirm } from './IntakeConfirm'
import { ConstraintRepairPanel } from './ConstraintRepairPanel'
import { SlotChecklist } from './SlotChecklist'
import { TripBoard } from './TripBoard'
import type { useHomePlanning } from './useHomePlanning'
import type { IntakeChat } from './useIntakeChat'

const STEPS = ['补信息', '确认', '生成中', '完成'] as const

/** 生成期四段阶段进度：SSE 推进 planning.progress 0→4（消费侧在
 * useHomePlanning.monitor，本组件只读展示）。 */
const PLANNING_STAGES = ['理解想法', '查找信息', '安排路线', '核对行程']

/** 天卡里最多亮出的地点芯片数，多出的收成「+N」 */
const DAY_CHIP_LIMIT = 5

/** active 右栏的面板态：collecting/confirm、generating 与 pending/error/login
 * 兜底态在本组件落位（生成态+兜底态原 home/TripPreview.tsx 已就地迁入）；done 态
 * （ready）接线 TripBoard 预览板（卡5，拍板①：留在首页看预览）。 */
type TripPanelState = 'collecting' | 'confirm' | 'generating' | 'ready' | 'pending' | 'error' | 'login'

/** 四步 stepper（PLAN §3.1）的当前步：error/login 回到「确认」等重开，
 * pending 卡在「生成中」，ready 才算「完成」。 */
const STEP_INDEX: Record<TripPanelState, number> = {
  collecting: 0,
  confirm: 1,
  generating: 2,
  pending: 2,
  ready: 3,
  error: 1,
  login: 1,
}

/** active 态右栏容器：常驻四步 stepper + 当态内容区。生成压过对话态
 * （planning.busy 优先），status idle 时由对话就绪度分流收集/确认。
 * confirm 态就地展示出发前确认表单（IntakeConfirm），平衡左右分栏权重。
 * 动效：stepper 进度轨随步推进；当态内容区按 state 换 key 只做入场（淡入上浮），
 * 不做退场——旧内容即时卸载，测试与读屏都不会看到两态并存。 */
export function TripPanel({ planning, chat, onStart }: {
  planning: ReturnType<typeof useHomePlanning>
  chat: IntakeChat
  onStart: (input: GenerateInput) => void
}) {
  const state: TripPanelState =
    planning.status === 'creating' || planning.status === 'planning'
      ? 'generating'
      : planning.status === 'idle'
        ? (chat.ready || chat.needsReconfirm ? 'confirm' : 'collecting')
        : planning.status
  const step = STEP_INDEX[state]
  const showBoard = state === 'ready' && planning.draft && planning.draft.days > 0
  // error 态在对话仍就绪时回到确认卡；其余 error 露出兜底文案
  const showConfirm = state === 'confirm' || (state === 'error' && (chat.ready || chat.needsReconfirm))
  const contentKey = showBoard ? 'board' : state === 'error' && showConfirm ? 'error-confirm' : state
  return <section className="trip-panel" aria-label="行程工作面板" aria-busy={planning.busy}>
    <TripSteps step={step} busy={planning.busy} partial={planning.draft?.genState === 'PARTIAL'} />
    <motion.div className="trip-panel-body" key={contentKey} variants={fadeUp} initial="hidden" animate="show">
      {!planning.busy && planning.draft && chat.serverState?.requirements && <ConstraintRepairPanel
        trip={planning.draft} requirements={chat.serverState.requirements}
        onAdopt={next => { chat.updateRequirements(next); planning.reset() }}
      />}
      {state === 'collecting' && <SlotChecklist slots={chat.slots} onSelectPrompt={(text) => void chat.send(text)} />}
      {showConfirm && (
        <IntakeConfirm
          slots={chat.slots}
          requirements={chat.serverState?.requirements}
          busy={chat.sending || planning.busy}
          ready={chat.ready}
          needsReconfirm={chat.needsReconfirm}
          onSlots={chat.updateSlots}
          onRequirements={chat.updateRequirements}
          onReset={chat.reset}
          onReconfirm={chat.reconfirm}
          onStart={() => onStart(toGenerateInput(chat.slots, chat.firstMessage, chat.serverState?.requirements))}
        />
      )}
      {/* done 态（ready 且 draft 可用）上预览板；ready 但 draft 异常缺失的退化情形
          留在 PlanningPreview 露出结果文案，不白屏。候选（M5a）顺手透传：终态已清，
          极端竞态下（ready 后仍有迟到候选帧）板上按「正在完善」呈现而非静默丢失 */}
      {showBoard
        ? <TripBoard draft={planning.draft!} previews={planning.previews} onReset={chat.reset} />
        : (state === 'generating' || state === 'ready' || state === 'pending' || state === 'error' || state === 'login')
          && <PlanningPreview planning={planning} />}
    </motion.div>
  </section>
}

/** 四步 stepper：底轨 + 随步推进的填充轨（弹簧），当前步圆点带呼吸环；
 * 外层 wrap 吸顶（顶栏下方），右栏内容长时进度总览常驻。
 * 类名契约：ol.trip-steps、li.is-done / li.is-current、aria-current="step"。 */
function TripSteps({ step, busy, partial }: { step: number; busy: boolean; partial: boolean }) {
  const fill = `${(step / (STEPS.length - 1)) * 100}%`
  // 轨道排在 ol 之前：两者都是定位元素，后者叠在上层，节点圆盖住轨道
  return <div className="trip-steps-wrap">
    <div className="trip-steps-track" aria-hidden="true">
      <motion.span className="trip-steps-fill" initial={false} animate={{ width: fill }} transition={SPRING_SNAPPY} />
    </div>
    <ol className="trip-steps" aria-label="规划进度">
      {STEPS.map((label, index) => {
        const done = index < step
        const current = index === step
        return <li key={label} className={done ? 'is-done' : current ? 'is-current' : ''} aria-current={current ? 'step' : undefined}>
          <span className="trip-step-node" aria-hidden="true">
            {done
              ? <motion.span className="trip-step-check" initial={{ scale: 0 }} animate={{ scale: 1 }} transition={SPRING_POP}><Icon name="check" size={12} strokeWidth={2.6} /></motion.span>
              : <span className={current && busy ? 'trip-step-dot is-busy' : 'trip-step-dot'} />}
          </span>
          <span className="trip-step-label">{index === 3 && partial ? '待补齐' : label}</span>
        </li>
      })}
    </ol>
  </div>
}

/** 生成/兜底态内容区（PLAN §3.4/§3.6，原 home/TripPreview.tsx 卡4 就地迁入）：
 * 总进度条 + 四段阶段（ol.planning-stages 是金路径 E2E 契约类，不许改名）+
 * 逐日生长卡（已排好的天亮主题+地点芯片依次弹出；正在排的天卡带流光边与
 * 在途候选芯片；排队的天虚线占位），generating/pending/error/login 同容器落位；
 * ready+draft 已由上面的 TripBoard 接走，此处仅剩 draft 异常缺失的退化兜底。
 * 数据来自 SSE + 轮询对账的 draft（useHomePlanning），无需独立请求。 */
function PlanningPreview({ planning }: { planning: ReturnType<typeof useHomePlanning> }) {
  const { status, message, progress, draft, busy, previews, coreReady, degradedNote, cancelling, cancel } = planning
  const researchCandidates = candidatesForDay(previews, 0).filter(candidate => candidate.item.poiName)
  const doneDays = draft ? draft.dayList.filter((day) => dayDone(day)).length : 0
  // 总进度：阶段占前 60%，逐日交付占后 40%——天数越多，后段越能看出在动
  const stagePart = Math.min(progress, PLANNING_STAGES.length) / PLANNING_STAGES.length
  const dayPart = draft && draft.days > 0 ? doneDays / draft.days : 0
  const percent = status === 'ready' ? 100 : Math.max(6, Math.round((stagePart * 0.6 + dayPart * 0.4) * 100))
  return <aside className="trip-preview" aria-label="行程实时预览" aria-busy={busy}>
    <div className={`planning-hero${busy ? ' is-busy' : ''}`}>
      <div className="planning-result-heading">
        <span aria-hidden="true" className={busy ? 'planning-compass' : `planning-result-icon is-${status}`}>
          {busy ? <Icon name="compass" size={20} /> : <Icon name={status === 'ready' ? 'check' : 'alert'} size={19} />}
        </span>
        <div className="planning-result-copy">
          <p role={status === 'error' || status === 'login' ? 'alert' : 'status'}>{message}</p>
          {busy && draft && draft.days > 0 && <small>已完成 {doneDays}/{draft.days} 天</small>}
        </div>
        {busy && <span className="planning-percent" aria-hidden="true">{percent}%</span>}
      </div>
      {busy && <div className="planning-bar" aria-hidden="true">
        <motion.span className="planning-bar-fill" initial={false} animate={{ width: `${percent}%` }} transition={SPRING_LAYOUT} />
      </div>}
      {busy && <ol className="planning-stages">
        {PLANNING_STAGES.map((stage, index) => (
          <li key={stage} className={index < progress ? 'is-active is-passed' : index === progress ? 'is-active is-now' : ''} aria-current={index === progress ? 'step' : undefined}>
            {index < progress
              ? <motion.span className="planning-stage-check" initial={{ scale: 0 }} animate={{ scale: 1 }} transition={SPRING_POP}><Icon name="check" size={11} strokeWidth={2.6} /></motion.span>
              : <span className="planning-stage-mark" />}
            {stage}
          </li>
        ))}
      </ol>}
    </div>
    {/* T1 持久取消旗的产品入口：按下只是"请服务端停手"，监控继续把真实终态读回来——
        已排好的天保留为 PARTIAL，所以这里不能把按钮按下当成"已取消"。
        非 busy 态不出现（没有可取消的进行中任务，后端也会回 409）。 */}
    {busy && <button className="button button-secondary" type="button" disabled={cancelling} onClick={() => void cancel()}>
      {cancelling ? '正在取消' : '取消这次的规划'}
    </button>}
    {/* M5b（spec §11）：core_ready 非阻断轻提示——主行程已可查看，备选富化仍在进行；
        done/complete 收尾时提示随状态一并清（useHomePlanning），这里不需要再兜底 */}
    {coreReady && <p className="planning-core-hint" role="status"><Icon name="spark" size={13} />主行程已可查看，备选仍在完善</p>}
    {/* 外部信息没取到时不装作「全都齐了」；复用同一非阻断提示位，零新增样式 */}
    {busy && degradedNote && <p className="planning-core-hint" role="status"><Icon name="alert" size={13} />{degradedNote}</p>}
    {busy && researchCandidates.length > 0 && <section aria-label="尚未排程的研究候选">
      <p className="planning-core-hint">研究候选 · 尚未排程，费用与营业信息以规划说明为准</p>
      <ul className="trip-preview-candidates">{researchCandidates.map(candidate => <li key={candidate.previewId} className="trip-preview-candidate">
        <span className="trip-preview-candidate-name">{candidate.item.poiName}</span>
        <span className="trip-preview-candidate-tag">正在完善</span>
      </li>)}</ul>
    </section>}
    {status === 'login' && <button className="button button-primary" type="button" onClick={() => navigate(loginRedirect())}>登录并继续<Icon name="arrow" size={16} /></button>}
    {draft && draft.days > 0 && <motion.ol className="trip-preview-days" variants={staggerChildren(0.07)} initial="hidden" animate="show">
      {Array.from({ length: draft.days }, (_, index) => index + 1).map((dayNo) => (
        <DayGrowCard key={dayNo} dayNo={dayNo} draft={draft} busy={busy} previews={previews} />
      ))}
    </motion.ol>}
    {draft && (status === 'pending' || status === 'error') && <a className="button button-primary" href={`/trips/${draft.id}`}>查看当前行程<Icon name="arrow" size={16} /></a>}
    {status === 'error' && <p className="planning-retry-hint">对话里的信息还在，可在确认卡里再次「开始规划」，或先继续对话调整条件。</p>}
  </aside>
}

function dayDone(day: DayPlan | undefined) {
  return Boolean(day && day.items.length > 0 && (!day.generationStatus || day.generationStatus === 'SUCCEEDED'))
}

/** 单天「生长卡」：排队（虚线）→ 正在规划（流光边 + 在途候选芯片）→ 完成
 * （勾选弹出 + 主题 + 地点芯片依次长出）。layout 让卡片高度变化平滑过渡。 */
function DayGrowCard({ dayNo, draft, busy, previews }: {
  dayNo: number
  draft: ItineraryDetail
  busy: boolean
  previews: ItemPreviewCandidate[]
}) {
  const day = draft.dayList.find((item) => item.dayNo === dayNo)
  const done = dayDone(day)
  // 已有条目但状态回到 RUNNING/PENDING = 后端在终检/重排这一天：保留已长出的地点
  // （置灰 +「正在校对」），不让用户看到进度「倒退」回骨架
  const verifying = !done && Boolean(day && day.items.length > 0)
  const isRunning = day?.generationStatus === 'RUNNING'
  const firstUnfinished = draft.dayList.find((item) => !item.items.length)?.dayNo
  const isCurrentGenerating = busy && !done && (isRunning || dayNo === firstUnfinished)
  const candidates = candidatesForDay(previews, dayNo)
  const named = candidates.filter((candidate) => candidate.item.poiName)
  const unnamed = candidates.length - named.length
  const stateClass = done ? 'is-done' : verifying ? 'is-pending is-verifying' : `is-pending${isCurrentGenerating ? ' is-generating' : ' is-queued'}`
  return <motion.li className={stateClass} variants={fadeUp} layout="position" transition={SPRING_LAYOUT}>
    <div className="trip-preview-dayhead">
      <span className="trip-preview-dayno">
        {done
          ? <motion.span className="trip-preview-daycheck" initial={{ scale: 0, rotate: -30 }} animate={{ scale: 1, rotate: 0 }} transition={SPRING_POP}><Icon name="check" size={12} strokeWidth={2.6} /></motion.span>
          : <span className="trip-preview-dayindex">{dayNo}</span>}
        第 {dayNo} 天
      </span>
      {!done && <span className={`trip-preview-status-pill${isCurrentGenerating || (verifying && busy) ? ' is-active' : ''}`}>{!busy ? '尚未完成' : verifying ? '正在校对' : isCurrentGenerating ? '正在规划中' : '排队中'}</span>}
    </div>
    {(done || verifying) && day
      ? <div className="trip-preview-daybody">
          <motion.strong initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.35, ease: EASE_OUT }}>{day.theme || '当天安排'}</motion.strong>
          <motion.ul className="trip-preview-pois" variants={staggerChildren(0.08, 0.12)} initial="hidden" animate="show" aria-label={`第 ${dayNo} 天地点`}>
            {day.items.slice(0, DAY_CHIP_LIMIT).map((item, index) => (
              <motion.li key={`${item.poiName}-${index}`} className={`trip-preview-poi is-${item.itemType || 'other'}`} variants={chipPop}>{item.poiName}</motion.li>
            ))}
            {day.items.length > DAY_CHIP_LIMIT && <motion.li className="trip-preview-poi is-more" variants={chipPop}>+{day.items.length - DAY_CHIP_LIMIT}</motion.li>}
          </motion.ul>
        </div>
      : <div className="trip-preview-daybody">
          <strong>{busy ? '安排中…' : '当天内容尚未完成'}</strong>
          <small>{!busy ? '可在详情页查看已有内容并重试' : isCurrentGenerating ? '司南正在排这一天的路线' : '排在队列里，马上就好'}</small>
          {named.length > 0
            ? <ul className="trip-preview-candidates">
                {named.map((candidate) => <motion.li key={candidate.previewId} className="trip-preview-candidate" variants={chipPop} initial="hidden" animate="show">
                  <span className="trip-preview-candidate-name">{candidate.item.poiName}</span>
                  <span className="trip-preview-candidate-tag">{candidate.officialItemId ? '已并入' : '正在完善'}</span>
                </motion.li>)}
                {unnamed > 0 && <li className="trip-preview-candidate is-unnamed">还在挑选 {unnamed} 个地点…</li>}
              </ul>
            : isCurrentGenerating
              ? <div className="trip-preview-shimmer" aria-hidden="true">
                  {unnamed > 0
                    ? Array.from({ length: Math.min(unnamed, DAY_CHIP_LIMIT) }, (_, index) => <span key={index} className="shimmer-chip" style={{ animationDelay: `${index * 120}ms` }} />)
                    : <><span className="shimmer-line shimmer-line-long" /><span className="shimmer-line shimmer-line-mid" /></>}
                </div>
              : null}
        </div>}
  </motion.li>
}
