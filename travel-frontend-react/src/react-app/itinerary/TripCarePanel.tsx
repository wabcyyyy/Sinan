import { useCallback, useEffect, useRef, useState } from 'react'
import type * as Contracts from '../../types/generated/contracts'
import {
  dismissTripCare,
  getTripCare,
  requestTripCareCheck,
  updateTripCare,
} from '../../api/sinan'
import { Icon } from '../shared/Icon'
import type { DayPlan } from '../../types/itinerary'

/**
 * 出发前照看面板（个人 Agent V1，规格 §5）：详情页的开关、关注活动选择、
 * 后台 AI 费用说明、立即检查与结果卡。
 *
 * 边界：
 * - 状态唯一真源在服务端（itinerary_care 表）——面板只投影 GET 的返回，不本地
 *   推演任务状态；supported=false（功能关闭/行程不具备资格）整块隐藏；
 * - 只在真实模式请求 care API：demo/离线样例由调用方用 carePanelVisible 门住，
 *   组件内部不再请求（规格 §5「静态展示版不调用新接口、不伪装实时照看」）；
 * - 页面加载与回到前台获取状态；仅 status=checking 时短轮询，离页/状态离开即清理；
 * - 「查看调整草稿」跳既有聊天草稿（ChatPanel 的当前唯一待确认卡），确认/忽略后
 *   由调用方 bump refreshKey 重取状态——不造第二套草稿语义。
 */

/** 监控范围上限（与后端 itinerary_care._MAX_WATCHED 同一规则，仅提示用）。 */
const MAX_WATCHED = 20
/** checking 短轮询间隔：检查是分钟级后台工作，5s 足够跟状态。 */
const POLLING_MS = 5000

/** 面板可见性（静态展示隔离的唯一判定）：离线样例与演示模式都不渲染、不请求。 */
export function carePanelVisible(options: { offline: boolean; demo: boolean }): boolean {
  return !options.offline && !options.demo
}

/** 任务状态的固定文案（结果卡之外的状态行）；结果语义固定文案见 careResultLine。 */
const STATUS_COPY: Record<string, string> = {
  checking: '正在检查天气…',
  waiting: '将在出发前三天开始检查',
  needs_reselect: '关注的活动已不在行程中，请重新选择',
  deferred: '今日已尝试过自动建议，明天继续',
  failed: '最近一次检查未完成，将等待下一窗口',
  finished: '行程已出发或已归档，照看结束',
  account_disabled: '账号已停用，照看暂停',
}

/** 草稿现状派生态（规格 §4.3）：lastResult.status 在有 pendingMessageId 时由后端
 * 覆写为 pending/applied/stale（与 apply 链同源判定），前端据此出结果卡主行。 */
export type CareDraftState = 'pending' | 'applied' | 'stale'

export function careDraftState(result: Contracts.CareCheckResult | null): CareDraftState | null {
  if (!result) return null
  if (result.status === 'pending' || result.status === 'applied' || result.status === 'stale') return result.status
  return null
}

/** 草稿现状的主行文案。stale/applied 是按消息现状的如实陈述——过期的建议绝不
 * 显示「已成功调整」（规格 §5）；applied 是「已消费」的派生态（确认应用与忽略
 * 在服务端落为同一现状，见 itinerary_chat.consume_pending_action），文案对两种
 * 结局都成立，不冒认用户没做过的「已应用」。 */
export const CARE_DRAFT_COPY: Record<CareDraftState, string> = {
  pending: '有一份天气调整建议待你确认',
  applied: '该建议已处理，不再是待确认草稿',
  stale: '该建议已失效，行程未按它调整',
}

/** 结果语义的固定文案（规格 §5 三条硬性口径优先，其余用后端 reason，缺省落状态文案）。 */
export function careResultLine(result: Contracts.CareCheckResult): string {
  if (result.status === 'unavailable') return '暂未取得天气信息'
  if (result.status === 'waiting_for_user') return '先处理当前草稿'
  if (result.status === 'quota_exhausted') return '未生成调整建议：额度不足'
  return result.reason || STATUS_COPY[result.status] || '最近一次检查已完成'
}

export interface CareItemRef {
  id: number
  name: string
  dayNo: number
}

/** 当前行程里仍存在的 attraction（供选择芯片）：按天次排序，缺 id/软删行不出现。 */
export function careAttractions(dayList: DayPlan[]): CareItemRef[] {
  const out: CareItemRef[] = []
  for (const day of [...dayList].sort((a, b) => a.dayNo - b.dayNo)) {
    for (const item of day.items || []) {
      if (item.itemType === 'attraction' && typeof item.id === 'number' && item.id > 0) {
        out.push({ id: item.id, name: item.poiName || `第 ${day.dayNo} 天活动`, dayNo: day.dayNo })
      }
    }
  }
  return out
}

/** 受影响活动名（affectedItemIds → 当前行程点位）：死 ID（行程编辑后删除）不渲染，
 * 不按名称猜绑定（规格 §3.2）。 */
export function careAffectedNames(dayList: DayPlan[], ids: number[]): string[] {
  const byId = new Map(careAttractions(dayList).map((item) => [item.id, item.name]))
  const seen: string[] = []
  for (const id of ids) {
    const name = byId.get(id)
    if (name && !seen.includes(name)) seen.push(name)
  }
  return seen
}

export interface CareRiskRow {
  date: string
  condition: string
  precip: string | null
}

/** 预报依据行：仅风险日（risk=true）；条件缺省如实写「预报有降水风险」，不编造。 */
export function careRiskRows(result: Contracts.CareCheckResult): CareRiskRow[] {
  return (result.days || [])
    .filter((day) => day.risk)
    .map((day) => ({
      date: day.date,
      condition: day.condition || '预报有降水风险',
      precip: day.precipProb == null ? null : `${Math.round(day.precipProb)}%`,
    }))
}

/** 服务端存的是应用侧上海墙钟时刻，原样展示（不做时区换算，避免本地时区漂移）。 */
export function formatCareTime(value: string | null | undefined): string | null {
  if (!value) return null
  const text = value.slice(0, 16).replace('T', ' ')
  return text || null
}

export function formatCareDay(value: string | null | undefined): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || '')
  return match ? `${Number(match[2])}月${Number(match[3])}日` : value || ''
}

export interface CarePanelViewProps {
  care: Contracts.CareStateVO
  dayList: DayPlan[]
  selection: number[]
  /** 关注范围有未保存改动（开启前的选择随开启一起保存；已开启时单独保存） */
  dirty: boolean
  agree: boolean
  busy: '' | 'save' | 'check' | 'dismiss'
  notice: string
  onToggleAgree: (value: boolean) => void
  onToggleItem: (id: number) => void
  onEnable: () => void
  onDisable: () => void
  onSaveSelection: () => void
  onCheck: () => void
  onDismiss: () => void
  onViewDraft: () => void
  onNoticeClose: () => void
}

/** 面板的纯投影（导出仅供单测静态渲染）：care 状态 + 本地编辑态 → 标记。 */
export function CarePanelView(props: CarePanelViewProps) {
  const { care, dayList, selection, dirty, agree, busy, notice } = props
  const attractions = careAttractions(dayList)
  const selectedSet = new Set(selection)
  const draftState = careDraftState(care.lastResult)
  const resultLine = care.lastResult ? careResultLine(care.lastResult) : null
  const riskRows = care.lastResult ? careRiskRows(care.lastResult) : []
  const affectedNames = care.lastResult ? careAffectedNames(dayList, care.lastResult.affectedItemIds || []) : []
  const checkedAt = formatCareTime(care.lastCheckedAt || care.lastResult?.checkedAt)
  const nextCheckAt = formatCareTime(care.nextCheckAt)
  // 不可用的观测不展示风险行：那批 days 是上一轮残留观测，配上「暂未取得天气信息」会误导
  const showRiskRows = care.lastResult?.status !== 'unavailable' && riskRows.length > 0

  return <section className="care-panel" aria-label="出发前照看">
    <div className="care-panel-head">
      <div>
        <h2><Icon name="sun" size={16} />出发前照看</h2>
        <p>出发前三天开始检查天气；发现影响关注活动的雨雪风险时，先给你一份可确认的调整草稿。</p>
      </div>
      <label className="care-switch">
        <input
          type="checkbox"
          checked={care.enabled}
          disabled={busy !== ''}
          aria-label={care.enabled ? '关闭出发前照看' : '开启出发前照看'}
          onChange={() => (care.enabled ? props.onDisable() : props.onEnable())}
        />
        <span>{care.enabled ? '已开启' : '已关闭'}</span>
      </label>
    </div>

    {!care.enabled && <div className="care-enable-box">
      <p className="care-cost-note">
        <Icon name="spark" size={14} />
        <span>开启后，司南会定期检查关注活动的天气；发现雨雪风险时可能在后台生成调整草稿——这一步会按现有额度机制消耗少量 AI 额度。</span>
      </p>
      <label className="care-agree">
        <input type="checkbox" checked={agree} disabled={busy !== ''} onChange={(event) => props.onToggleAgree(event.target.checked)} />
        <span>我同意后台 AI 建议可能消耗额度</span>
      </label>
    </div>}

    <div className="care-watch">
      <span className="care-watch-label">关注这些活动的天气影响（最多 {MAX_WATCHED} 个）</span>
      {attractions.length ? <div className="care-chips">
        {attractions.map((item) => {
          const on = selectedSet.has(item.id)
          return <button
            key={item.id}
            type="button"
            className={on ? 'care-chip is-on' : 'care-chip'}
            aria-pressed={on}
            disabled={!on && busy !== ''}
            title={`第 ${item.dayNo} 天 · ${item.name}`}
            onClick={() => props.onToggleItem(item.id)}
          >
            <span>D{item.dayNo}</span>{item.name}
          </button>
        })}
      </div> : <p className="care-watch-empty">这一趟还没有可关注的景点安排。</p>}
      {care.enabled && dirty && <button className="text-action care-save-range" type="button" disabled={busy !== ''} onClick={props.onSaveSelection}>保存关注范围</button>}
    </div>

    {care.needsReselect && <p className="care-warning" role="alert"><Icon name="alert" size={14} />关注的活动已不在行程中，请重新选择后保存。</p>}

    {care.enabled && <div className="care-actions">
      <button className="button button-secondary" type="button" disabled={busy !== '' || !care.supported} onClick={props.onCheck}>
        <Icon name="refresh" size={15} />{busy === 'check' ? '正在检查…' : '立即检查'}
      </button>
      {care.status === 'checking' && <span className="care-status is-checking" role="status">正在检查天气…</span>}
      {care.status !== 'checking' && nextCheckAt && <span className="care-next">下次检查 {nextCheckAt}</span>}
    </div>}

    {care.lastResult && <div className="care-result">
      <div className="care-result-head">
        <strong>最近检查</strong>
        {checkedAt && <span>{checkedAt} 检查</span>}
      </div>
      {draftState && <p className={`care-result-line${draftState === 'pending' ? ' is-active' : ''}`}>{CARE_DRAFT_COPY[draftState]}</p>}
      {!draftState && resultLine && !care.needsReselect && <p className="care-result-line">{resultLine}</p>}
      {showRiskRows && <ul className="care-risk-list" aria-label="预报依据">
        {riskRows.map((row) => <li key={row.date}>
          <strong>{formatCareDay(row.date)}</strong>
          <span>{row.condition}{row.precip ? ` · 降水概率 ${row.precip}` : ''}</span>
        </li>)}
      </ul>}
      {draftState === 'pending' && affectedNames.length > 0 && <p className="care-affected">受影响：{affectedNames.join('、')}</p>}
      <p className="care-result-foot">预报是查询时点的数据（可能来自缓存），出发前请以最新预报为准。</p>
      {draftState === 'pending' && <div className="care-result-actions">
        <button className="button button-primary" type="button" onClick={props.onViewDraft}>查看调整草稿</button>
        <button className="button button-secondary" type="button" disabled={busy === 'dismiss'} onClick={props.onDismiss}>
          {busy === 'dismiss' ? '正在忽略…' : '忽略'}
        </button>
      </div>}
    </div>}

    {care.enabled && !care.lastResult && care.status !== 'checking' && <p className="care-status">已开启，等待首次检查</p>}

    {notice && <p className="care-notice" role="alert">
      {notice}
      <button type="button" aria-label="关闭提示" onClick={props.onNoticeClose}><Icon name="close" size={13} /></button>
    </p>}
  </section>
}

/** 详情页挂载的有状态壳：负责取数、轮询与动作；展示全部交给 CarePanelView。 */
export function TripCarePanel({
  itineraryId,
  dayList,
  onViewDraft,
  refreshKey = 0,
}: {
  itineraryId: number
  dayList: DayPlan[]
  onViewDraft: () => void
  /** 调用方（草稿确认/忽略等外部事件）bump 此键触发重取状态 */
  refreshKey?: number
}) {
  const [care, setCare] = useState<Contracts.CareStateVO | null>(null)
  const [failed, setFailed] = useState(false)
  const [selection, setSelection] = useState<number[]>([])
  const [dirty, setDirty] = useState(false)
  const [agree, setAgree] = useState(false)
  const [busy, setBusy] = useState<'' | 'save' | 'check' | 'dismiss'>('')
  const [notice, setNotice] = useState('')
  const seqRef = useRef(0)
  const dirtyRef = useRef(false)
  dirtyRef.current = dirty

  const applyState = useCallback((next: Contracts.CareStateVO) => {
    setCare(next)
    if (!dirtyRef.current) setSelection(next.watchedItemIds || [])
  }, [])

  const refresh = useCallback(async () => {
    const seq = ++seqRef.current
    try {
      const next = await getTripCare(itineraryId)
      if (seq !== seqRef.current) return
      applyState(next)
      setFailed(false)
    } catch {
      // 状态面失败不阻塞详情页：整块隐藏，回到前台/外部事件时再试
      if (seq === seqRef.current) setFailed(true)
    }
  }, [itineraryId, applyState])

  // 页面加载 + 外部事件（草稿确认等）触发重取
  useEffect(() => { void refresh() }, [refresh, refreshKey])

  // 回到前台获取状态（规格 §5）
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [refresh])

  // 仅 checking 时短轮询，状态离开 checking 或离页即清理（规格 §5）
  useEffect(() => {
    if (care?.status !== 'checking') return
    const timer = window.setInterval(() => { void refresh() }, POLLING_MS)
    return () => window.clearInterval(timer)
  }, [care?.status, refresh])

  // toast 自灭：4s 到点收敛，手动关闭钮保留
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  const writeError = (err: unknown, fallback: string) => {
    setNotice(err instanceof Error && err.message ? err.message : fallback)
  }

  const toggleItem = (id: number) => {
    setNotice('')
    setSelection((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id)
      if (current.length >= MAX_WATCHED) {
        setNotice(`最多关注 ${MAX_WATCHED} 个活动的天气影响`)
        return current
      }
      return [...current, id]
    })
    setDirty(true)
  }

  const enable = async () => {
    if (busy) return
    if (!selection.length) { setNotice('请先选择需要关注天气影响的活动'); return }
    if (!agree) { setNotice('开启前请先勾选同意：后台 AI 建议可能消耗额度'); return }
    setBusy('save')
    try {
      applyState(await updateTripCare(itineraryId, { enabled: true, watchedItemIds: selection, allowBackgroundAi: true }))
      setDirty(false)
      setAgree(false)
      setNotice('')
    } catch (err) {
      writeError(err, '开启失败，请稍后再试')
    } finally { setBusy('') }
  }

  const disable = async () => {
    if (busy) return
    setBusy('save')
    try {
      // 关闭保留已呈现结果供查看/忽略（后端语义）；本地选择保留，重新开启可沿用
      applyState(await updateTripCare(itineraryId, { enabled: false, watchedItemIds: null, allowBackgroundAi: null }))
      setNotice('')
    } catch (err) {
      writeError(err, '关闭失败，请稍后再试')
    } finally { setBusy('') }
  }

  const saveSelection = async () => {
    if (busy) return
    if (!selection.length) { setNotice('请先选择需要关注天气影响的活动'); return }
    setBusy('save')
    try {
      // 只更新范围（enabled/allowBackgroundAi 传 null=不动）：不重置检查节奏（规格 §3.3 用户重选可重算范围）
      applyState(await updateTripCare(itineraryId, { enabled: null, watchedItemIds: selection, allowBackgroundAi: null }))
      setDirty(false)
      setNotice('')
    } catch (err) {
      writeError(err, '保存失败，请稍后再试')
    } finally { setBusy('') }
  }

  const checkNow = async () => {
    if (busy) return
    setBusy('check')
    try {
      applyState(await requestTripCareCheck(itineraryId))
      setNotice('')
    } catch (err) {
      writeError(err, '检查请求失败，请稍后再试')
    } finally { setBusy('') }
  }

  const dismiss = async () => {
    if (busy === 'dismiss') return
    setBusy('dismiss')
    try {
      applyState(await dismissTripCare(itineraryId))
      setNotice('')
    } catch (err) {
      writeError(err, '忽略失败，请稍后再试')
    } finally { setBusy('') }
  }

  if (!care || failed) return null
  if (!care.supported) {
    // 已出发/已归档（status=finished）：操作面板不再渲染，但给一句终态交代——
    // 状态真源在服务端，这里只投影（规格 §5）
    if (care.status !== 'finished') return null
    return <section className="care-panel care-panel-ended" aria-label="出发前照看">
      <p className="care-status">{STATUS_COPY.finished}</p>
    </section>
  }

  return <CarePanelView
    care={care}
    dayList={dayList}
    selection={selection}
    dirty={dirty}
    agree={agree}
    busy={busy}
    notice={notice}
    onToggleAgree={setAgree}
    onToggleItem={toggleItem}
    onEnable={() => void enable()}
    onDisable={() => void disable()}
    onSaveSelection={() => void saveSelection()}
    onCheck={() => void checkNow()}
    onDismiss={() => void dismiss()}
    onViewDraft={onViewDraft}
    onNoticeClose={() => setNotice('')}
  />
}
