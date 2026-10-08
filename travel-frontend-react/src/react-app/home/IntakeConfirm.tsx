import { useState } from 'react'
import type { ChangeEvent } from 'react'
import { fallbackCities } from '../data'
import { Icon } from '../shared/Icon'
import { INTAKE_PREFERENCES } from './intakeSlots'
import type { IntakeSlots } from './intakeSlots'

/** 出发前确认卡（PLAN §3.3，落位 active 右栏 TripPanel）：
 * 优先展示 AI 对话梳理生成的「行程方案就绪卡」，实现「对话完成一切，
 * 减少用户对标签按钮的繁琐操作」；同时提供折叠的轻量微调面板，
 * 兼顾极致对话体验与精准手工修改。
 * M2（spec §7.2）就绪门后置：ready/needsReconfirm 由后端 clarify 权威给出——
 * 表单改动后（needsReconfirm）开工键先走 onReconfirm 后端重验，ready 恢复才
 * 触发 onStart；重验未过保持禁用，任何前端路径都不在 !ready 时触发生成。 */
export function IntakeConfirm({
  slots,
  busy,
  ready,
  needsReconfirm,
  onSlots,
  onReset,
  onReconfirm,
  onStart,
}: {
  slots: IntakeSlots
  busy: boolean
  ready: boolean
  needsReconfirm: boolean
  onSlots: (patch: Partial<IntakeSlots>) => void
  onReset: () => void
  onReconfirm: () => Promise<boolean>
  onStart: () => void
}) {
  const [showDrawer, setShowDrawer] = useState(false)
  // 重验在途：与 busy 一起禁用开工键，防重入（重验本身也是一轮 clarify）
  const [verifying, setVerifying] = useState(false)
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
      <div className="plan-brief-card" aria-label="AI 为你整理的行程方案">
        <div className="plan-brief-hero">
          <div className="plan-brief-dest">
            <span className="plan-brief-city">{city}</span>
            <span className="plan-brief-scale">
              {days} 天 · {persons} 人同行
            </span>
          </div>
          {slots.budget && slots.budget > 0 ? (
            <span className="plan-brief-budget">预算 ¥{slots.budget}</span>
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
    </div>
  )
}
