import { useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import type { MemoryVO, TripRequirements } from '../../types/generated/contracts'
import { getPreferenceMemory, savePreferenceMemory } from '../../api/sinan'
import { isDemoMode } from '../../demoMode'
import { destinations, fallbackCities } from '../data'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'
import { INTAKE_PREFERENCES } from './intakeSlots'
import type { IntakeSlots } from './intakeSlots'
import { FixedArrangementsEditor } from './FixedArrangementsEditor'
import { RequirementSummary } from './RequirementSummary'
import { ConstraintPlanningEditor } from './ConstraintPlanningEditor'
import { ScreenshotImport } from '../shared/ScreenshotImport'

/** 长期记忆的边界（与后端 preferences 服务同一规则，仅前端提示用）：
 * 每项 trim 后 1–32 字符，正负合计最多 20 项。 */
export const MEMORY_MAX_CHARS = 32
export const MEMORY_MAX_TOTAL = 20

/** 追加一条长期偏好标签：trim、去空、去重、超长拒绝、正负合计上限（规格 §3.1.2）。
 * `otherCount` 是另一侧的条数——上限按正负合计计。返回 [新列表, 错误文案]。 */
export function addMemoryLabel(list: string[], raw: string, otherCount = 0): [string[], string] {
  const label = raw.trim()
  if (!label) return [list, '请先输入偏好内容']
  if (label.length > MEMORY_MAX_CHARS) return [list, `偏好标签最长 ${MEMORY_MAX_CHARS} 个字符`]
  if (list.includes(label)) return [list, '这个标签已经在列表里']
  if (list.length + otherCount >= MEMORY_MAX_TOTAL) return [list, `正负合计最多 ${MEMORY_MAX_TOTAL} 项`]
  return [[...list, label], '']
}

/** 出发前确认卡（PLAN §3.3，落位 active 右栏 TripPanel）：
 * 优先展示 AI 对话梳理生成的「行程方案就绪卡」，实现「对话完成一切，
 * 减少用户对标签按钮的繁琐操作」；同时提供折叠的轻量微调面板，
 * 兼顾极致对话体验与精准手工修改。
 * M2（spec §7.2）就绪门后置：ready/needsReconfirm 由后端 clarify 权威给出——
 * 表单改动后（needsReconfirm）开工键先走 onReconfirm 后端重验，ready 恢复才
 * 触发 onStart；重验未过保持禁用，任何前端路径都不在 !ready 时触发生成。 */
export function IntakeConfirm({
  slots,
  requirements,
  busy,
  ready,
  needsReconfirm,
  onSlots,
  onRequirements,
  onReset,
  onReconfirm,
  onStart,
}: {
  slots: IntakeSlots
  requirements?: TripRequirements | null
  busy: boolean
  ready: boolean
  needsReconfirm: boolean
  onSlots: (patch: Partial<IntakeSlots>) => void
  onRequirements?: (requirements: TripRequirements) => void
  onReset: () => void
  onReconfirm: () => Promise<boolean>
  onStart: () => void
}) {
  const [showDrawer, setShowDrawer] = useState(false)
  // 重验在途：与 busy 一起禁用开工键，防重入（重验本身也是一轮 clarify）
  const [verifying, setVerifying] = useState(false)

  // ---- 显式长期旅行记忆（个人 Agent V1，规格 §3.1.5/§5）：可展开、可编辑、可选入 ----
  // 展示模式（demo）整个入口不渲染、不请求（规格 §5「静态展示版不调用新接口」）
  const demoMode = isDemoMode()
  const [showMemory, setShowMemory] = useState(false)
  const [memory, setMemory] = useState<MemoryVO | null>(null)
  const [posDraft, setPosDraft] = useState<string[]>([])
  const [negDraft, setNegDraft] = useState<string[]>([])
  const [memoryText, setMemoryText] = useState('')
  const [memoryKind, setMemoryKind] = useState<'positive' | 'negative'>('positive')
  const [memoryBusy, setMemoryBusy] = useState(false)
  const [memoryNotice, setMemoryNotice] = useState('')
  const memoryLoadedRef = useRef(false)

  const openMemory = async () => {
    const next = !showMemory
    setShowMemory(next)
    if (!next || demoMode || memoryLoadedRef.current) return
    memoryLoadedRef.current = true
    setMemoryBusy(true)
    try {
      const vo = await getPreferenceMemory()
      setMemory(vo)
      setPosDraft(vo.positive || [])
      setNegDraft(vo.negative || [])
    } catch (err) {
      memoryLoadedRef.current = false
      setMemoryNotice(err instanceof Error && err.message ? err.message : '长期偏好读取失败')
    } finally {
      setMemoryBusy(false)
    }
  }

  const appendMemory = () => {
    if (memoryKind === 'positive') {
      const [list, error] = addMemoryLabel(posDraft, memoryText, negDraft.length)
      if (error) { setMemoryNotice(error); return }
      setPosDraft(list)
    } else {
      const [list, error] = addMemoryLabel(negDraft, memoryText, posDraft.length)
      if (error) { setMemoryNotice(error); return }
      setNegDraft(list)
    }
    setMemoryText('')
    setMemoryNotice('')
  }

  const removeMemoryLabel = (kind: 'positive' | 'negative', label: string) => {
    if (kind === 'positive') setPosDraft((list) => list.filter((item) => item !== label))
    else setNegDraft((list) => list.filter((item) => item !== label))
    setMemoryNotice('')
  }

  /** 保存/清空都是 PUT 原子替换：清空 = 空集合 = 遗忘此入口管理的全部记忆
   * （规格 §3.1.6，不动其他来源的历史标签）。 */
  const persistMemory = async (positive: string[], negative: string[]) => {
    setMemoryBusy(true)
    try {
      const vo = await savePreferenceMemory({ positive, negative })
      setMemory(vo)
      setPosDraft(vo.positive || [])
      setNegDraft(vo.negative || [])
      setMemoryNotice(positive.length || negative.length ? '长期偏好已保存，以后的行程可以复用' : '已清空长期偏好（历史行程不受影响）')
    } catch (err) {
      setMemoryNotice(err instanceof Error && err.message ? err.message : '保存失败，请稍后再试')
    } finally {
      setMemoryBusy(false)
    }
  }

  /** 选入本次行程：只在用户点击时进入本次 preferences（本次输入优先）；
   * 负向标签仅展示，不提供选入——不自动把负向标签升级为硬约束（规格 §3.1.5）。 */
  const adoptMemory = (label: string) => {
    if (preferences.includes(label)) return
    onSlots({ preferences: [...preferences, label] })
  }

  const preferences = slots.preferences || []
  const custom = preferences.filter((item) => !INTAKE_PREFERENCES.includes(item))
  const toggle = (item: string) => {
    onSlots({
      preferences: preferences.includes(item) ? preferences.filter((value) => value !== item) : [...preferences, item],
    })
  }
  const number = (key: 'days' | 'persons' | 'budget') => (event: ChangeEvent<HTMLInputElement>) => {
    const raw = event.target.value
    const parsed = Number(raw)
    onSlots({ [key]: raw !== '' && Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : undefined })
  }

  const startLocked = !ready && !needsReconfirm
  const handleStart = async () => {
    if (busy || verifying || startLocked) return
    if (ready && !needsReconfirm) {
      onStart()
      return
    }
    // needsReconfirm：先请后端重验当前表单，ready 恢复才开工；未过保持禁用
    setVerifying(true)
    let ok = false
    try {
      ok = await onReconfirm()
    } finally {
      setVerifying(false)
    }
    if (!ok) return
    onStart()
  }

  const city = slots.city?.trim() || '目的地'
  const days = slots.days || 3
  const persons = slots.persons || 2
  const hasDate = Boolean(slots.start_date)
  const hasPrefs = preferences.length > 0
  const cover = slots.city ? destinations.find((d) => slots.city?.includes(d.city) || d.city.includes(slots.city || ''))?.image : undefined

  return (
    <div className="intake-confirm trip-confirm">
      <div className="intake-confirm-head">
        <div className="intake-confirm-title">
          <h3>出发前确认</h3>
          <span className="intake-confirm-badge">
            <Icon name={needsReconfirm ? 'clock' : 'check'} size={12} />
            {needsReconfirm ? '信息已更新，待重新确认' : 'AI 对话已梳理就绪'}
          </span>
        </div>
        <button className="text-action" type="button" onClick={onReset} title="退出对话，返回首页">
          返回首页
        </button>
      </div>

      {/* 方案概览卡片（Plan Brief Card）：核心信息对话确认，免去手动按键 */}
      <div className={cover ? 'plan-brief-card has-cover' : 'plan-brief-card'} aria-label="AI 为你整理的行程方案">
        {cover && <div className="plan-brief-cover" aria-hidden="true"><SmartImg src={cover} alt="" ratio="16 / 6" /><span className="plan-brief-cover-scrim" /></div>}
        <div className="plan-brief-hero">
          <div className="plan-brief-dest">
            <span className="plan-brief-city">{city}</span>
            <span className="plan-brief-scale">
              {days} 天 · {persons} 人同行
            </span>
          </div>
          {slots.budget && slots.budget > 0 ? (
            <span className="plan-brief-budget">预算 {requirements?.constraintPlanning?.currency || 'CNY'} {slots.budget}</span>
          ) : null}
        </div>

        <div className="plan-brief-meta-grid">
          <div className="plan-brief-row">
            <span className="plan-brief-label">
              <Icon name="calendar" size={14} />
              出发时间
            </span>
            <span className={hasDate ? 'plan-brief-val is-highlight' : 'plan-brief-val is-faint'}>
              {hasDate ? `${slots.start_date} 出发` : '日期待定（出发前随时调整）'}
            </span>
          </div>
          {slots.origin_city && (
            <div className="plan-brief-row">
              <span className="plan-brief-label">
                <Icon name="train" size={14} />
                出发城市
              </span>
              <span className="plan-brief-val">{slots.origin_city}</span>
            </div>
          )}
          <div className="plan-brief-row plan-brief-prefs-row">
            <span className="plan-brief-label">
              <Icon name="compass" size={14} />
              玩法偏好
            </span>
            <div className="plan-brief-pills">
              {hasPrefs ? (
                preferences.map((item) => (
                  <span key={item} className="plan-brief-pill">
                    {item}
                  </span>
                ))
              ) : (
                <span className="plan-brief-pill is-default">经典全景游</span>
              )}
            </div>
          </div>
        </div>
      </div>

      {!demoMode && onRequirements && requirements && <FixedArrangementsEditor days={days} requirements={requirements} busy={busy || verifying} onChange={onRequirements} />}
      <RequirementSummary requirements={requirements} />
      {!demoMode && onRequirements && requirements && <ConstraintPlanningEditor requirements={requirements} days={days} busy={busy || verifying} onChange={onRequirements} />}
      {!demoMode && onRequirements && requirements && days <= 3 && <ScreenshotImport requirements={requirements} city={slots.city || ''} days={days} startDate={slots.start_date} busy={busy || verifying} onAdopt={next => onRequirements(next)} />}

      {/* 核心 CTA：用户一眼看清方案，直接开工；needsReconfirm 时先走后端重验 */}
      <button
        className="button button-primary intake-start"
        type="button"
        disabled={busy || verifying || startLocked}
        onClick={() => void handleStart()}
      >
        {needsReconfirm ? (verifying ? '正在重新确认…' : '信息已更新，重新确认') : '就这样，开始规划'}
        <Icon name="arrow" size={16} />
      </button>

      {/* 辅助微调抽屉（想改改细节？）：保留完整编辑能力与已有测试契约，但默认收起不抢戏 */}
      <div className="intake-tune-drawer">
        <button
          type="button"
          className="intake-tune-toggle"
          aria-expanded={showDrawer}
          onClick={() => setShowDrawer((v) => !v)}
        >
          <span>{showDrawer ? '收起微调参数' : '想手动修改参数或偏好？'}</span>
          <Icon name="arrow" size={12} className={showDrawer ? 'is-expanded' : ''} />
        </button>

        {showDrawer && (
          <div className="intake-tune-body">
            <div className="intake-grid">
              <label>
                目的地
                <input
                  value={slots.city || ''}
                  maxLength={80}
                  list="intake-cities"
                  onChange={(event) => onSlots({ city: event.target.value })}
                />
              </label>
              <label>
                天数
                <input type="number" min={1} max={7} value={slots.days ?? ''} onChange={number('days')} />
              </label>
              <label>
                人数
                <input type="number" min={1} max={20} value={slots.persons ?? ''} onChange={number('persons')} />
              </label>
              <label>
                出发日期
                <input
                  type="date"
                  value={slots.start_date || ''}
                  onChange={(event) => onSlots({ start_date: event.target.value || undefined })}
                />
              </label>
              <label>
                出发城市
                <input
                  value={slots.origin_city || ''}
                  maxLength={80}
                  placeholder="查航班用"
                  onChange={(event) => onSlots({ origin_city: event.target.value || undefined })}
                />
              </label>
              <label>
                全程预算
                <input
                  type="number"
                  min={1}
                  placeholder="例如 3000"
                  value={slots.budget ?? ''}
                  onChange={number('budget')}
                />
              </label>
            </div>
            <datalist id="intake-cities">
              {fallbackCities.map((city) => (
                <option key={city} value={city} />
              ))}
            </datalist>
            <div className="intake-prefs" aria-label="旅行偏好">
              {INTAKE_PREFERENCES.map((item) => (
                <button
                  key={item}
                  type="button"
                  aria-pressed={preferences.includes(item)}
                  onClick={() => toggle(item)}
                >
                  {item}
                </button>
              ))}
              {custom.map((item) => (
                <button
                  key={item}
                  type="button"
                  className="is-custom"
                  aria-pressed="true"
                  title="点选移除"
                  onClick={() => toggle(item)}
                >
                  {item}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>


      {/* 长期旅行偏好（个人 Agent V1，规格 §5）：与本次行程分离的显式记忆入口——
          可编辑正负标签、保存/清空、把正向偏好选入本次行程。展示模式整块不渲染。 */}
      {!demoMode && (
        <div className="intake-memory-drawer">
          <button
            type="button"
            className="intake-tune-toggle"
            aria-expanded={showMemory}
            onClick={() => void openMemory()}
          >
            <span>{showMemory ? '收起长期旅行偏好' : '有想长期记住的旅行偏好？'}</span>
            <Icon name="arrow" size={12} className={showMemory ? 'is-expanded' : ''} />
          </button>

          {showMemory && (
            <div className="intake-memory-body">
              {memory === null && !memoryNotice && <p className="intake-memory-empty">正在读取长期偏好…</p>}
              {memory !== null && <>
                <p className="intake-memory-hint">长期偏好独立于这一次行程：保存后可在以后的行程里复用；本次对话里的输入始终优先。</p>
                <div className="intake-memory-group">
                  <span className="intake-memory-side">喜欢</span>
                  <div className="intake-memory-pills">
                    {posDraft.length ? posDraft.map((label) => <MemoryChip
                      key={label}
                      label={label}
                      kind="positive"
                      adopted={preferences.includes(label)}
                      onAdopt={() => adoptMemory(label)}
                      onRemove={() => removeMemoryLabel('positive', label)}
                    />) : <span className="intake-memory-none">暂无</span>}
                  </div>
                </div>
                <div className="intake-memory-group">
                  <span className="intake-memory-side">避开</span>
                  <div className="intake-memory-pills">
                    {negDraft.length ? negDraft.map((label) => <MemoryChip
                      key={label}
                      label={label}
                      kind="negative"
                      adopted={false}
                      onRemove={() => removeMemoryLabel('negative', label)}
                    />) : <span className="intake-memory-none">暂无</span>}
                  </div>
                  <p className="intake-memory-note">「避开」只是提醒，不会自动变成这一次行程的硬性约束。</p>
                </div>
                <div className="intake-memory-editor">
                  <select value={memoryKind} aria-label="偏好方向" onChange={(event) => setMemoryKind(event.target.value === 'negative' ? 'negative' : 'positive')}>
                    <option value="positive">喜欢</option>
                    <option value="negative">避开</option>
                  </select>
                  <input
                    value={memoryText}
                    maxLength={MEMORY_MAX_CHARS}
                    placeholder="例如：徒步 / 不去主题乐园"
                    aria-label="新的长期偏好"
                    onChange={(event) => setMemoryText(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        appendMemory()
                      }
                    }}
                  />
                  <button type="button" className="button button-secondary" disabled={memoryBusy} onClick={appendMemory}>添加</button>
                </div>
                <div className="intake-memory-actions">
                  <button
                    type="button"
                    className="button button-primary"
                    disabled={memoryBusy}
                    onClick={() => void persistMemory(posDraft, negDraft)}
                  >
                    {memoryBusy ? '正在保存…' : '保存到长期偏好'}
                  </button>
                  <button
                    type="button"
                    className="text-action"
                    disabled={memoryBusy || (!posDraft.length && !negDraft.length)}
                    onClick={() => void persistMemory([], [])}
                  >
                    清空
                  </button>
                </div>
              </>}
              {memoryNotice && <p className="intake-memory-notice" role="alert">
                {memoryNotice}
                <button type="button" aria-label="关闭提示" onClick={() => setMemoryNotice('')}><Icon name="close" size={13} /></button>
              </p>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** 长期偏好芯片：正向可「选入本次行程」；移除仅改本地草稿，保存后才生效。 */
function MemoryChip({ label, kind, adopted, onAdopt, onRemove }: {
  label: string
  kind: 'positive' | 'negative'
  adopted: boolean
  onAdopt?: () => void
  onRemove: () => void
}) {
  return <span className={`intake-memory-chip is-${kind}`}>
    {label}
    {kind === 'positive' && onAdopt && (
      <button type="button" className="intake-memory-adopt" disabled={adopted} title="选入本次行程的玩法偏好" onClick={onAdopt}>
        {adopted ? '已选入' : '选入本次'}
      </button>
    )}
    <button type="button" className="intake-memory-remove" aria-label={`移除 ${label}`} onClick={onRemove}>
      <Icon name="close" size={11} />
    </button>
  </span>
}
