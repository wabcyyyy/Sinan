import { Icon } from '../shared/Icon'
import { destinations } from '../data'
import { SmartImg } from '../shared/SmartImg'
import { SLOT_DEFS, slotDisplayValue, slotIsFilled, slotProgress } from './intakeSlots'
import type { IntakeSlots } from './intakeSlots'

const PROMPT_SUGGESTIONS: Record<string, string[]> = {
  city: ['🏯 京都漫游', '🍜 成都美食', '🌊 大理环海', '🥐 巴黎人文', '🌴 巴厘岛度假'],
  days: ['玩 3 天精选', '安排 4 天深度', '打算玩 5 天', '周末 2 天放空'],
  persons: ['1个人自由行', '2人情侣出游', '3-4人好友结伴', '带父母家庭游'],
}

/** collecting 态右栏：信息收集与灵感意境画卷（对标 Layla.ai 去表单化）。
 * 必填三项随对话「长出来」——未填=优雅圆点、已填=✓+当前值；
 * 配合目的地画报卡与即点即发 Inspiration Chips；
 * 整块 role="status"，保持各项契约类名与读屏感知不变。 */
export function SlotChecklist({
  slots,
  onSelectPrompt,
}: {
  slots: IntakeSlots
  onSelectPrompt?: (text: string) => void
}) {
  const { requiredFilled, requiredTotal, nextRequiredKey } = slotProgress(slots)
  const remaining = requiredTotal - requiredFilled
  const matchedCity = slots.city
    ? destinations.find(
        (d) => slots.city?.includes(d.city) || d.city.includes(slots.city || ''),
      )
    : null

  const activeSuggestions = nextRequiredKey ? PROMPT_SUGGESTIONS[nextRequiredKey] : null

  return (
    <div className="slot-checklist" role="status" aria-label="信息收集进度">
      {/* 意境画报/目的地意向卡：将冷冰冰的表单转为向往感画报 */}
      {matchedCity ? (
        <div className="slot-hero-card">
          <div className="slot-hero-media">
            <SmartImg src={matchedCity.image} alt={matchedCity.city} ratio="2.4 / 1" />
            <div className="slot-hero-scrim" aria-hidden="true" />
            <div className="slot-hero-badge">
              <span className="slot-hero-tag">✦ 灵感目的地</span>
              <strong>{matchedCity.city} · {matchedCity.province}</strong>
            </div>
          </div>
          <p className="slot-hero-desc">{matchedCity.description}</p>
        </div>
      ) : (
        <div className="slot-mood-banner">
          <div className="slot-mood-lead">
            <span className="slot-mood-sparkle">✦</span>
            <div>
              <h4>定制专属行程灵感</h4>
              <p>说出你心仪的目的地或天数，司南将在此实时排布每日行程画卷</p>
            </div>
          </div>
        </div>
      )}

      <div className="slot-progress">
        <div className="slot-progress-track" aria-hidden="true">
          <div className="slot-progress-fill" style={{ width: `${Math.round((requiredFilled / requiredTotal) * 100)}%` }} />
        </div>
        <p className="slot-progress-text">
          {remaining > 0
            ? <><strong>{requiredFilled}/{requiredTotal}</strong> 还差 {remaining} 项就能开工</>
            : '必填信息齐了，回左侧确认就能开工'}
        </p>
      </div>

      <ul className="slot-required" aria-label="必填信息">
        {SLOT_DEFS.filter((def) => def.required).map((def) => {
          const filled = slotIsFilled(slots, def.key)
          const isNext = def.key === nextRequiredKey
          return (
            <li key={def.key} className={filled ? 'is-filled' : isNext ? 'is-next' : ''}>
              {filled ? <Icon name="check" size={14} /> : <span className="slot-dot" aria-hidden="true" />}
              <span>{def.label}</span>
              {filled && <span className="slot-value">{slotDisplayValue(slots, def.key)}</span>}
            </li>
          )
        })}
      </ul>

      {/* 快捷灵感选项芯片（Inspiration Chips：对话内自然点选推荐） */}
      {activeSuggestions && activeSuggestions.length > 0 && onSelectPrompt && (
        <div className="slot-inspiration-chips" aria-label="快捷灵感参考">
          <span className="slot-chips-label">试试快速点选：</span>
          <div className="slot-chips-row">
            {activeSuggestions.map((text) => (
              <button
                key={text}
                type="button"
                className="slot-chip-btn"
                onClick={() => onSelectPrompt(text)}
              >
                {text}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="slot-optional" aria-label="可选信息，想到就补充">
        {SLOT_DEFS.filter((def) => !def.required).map((def) => (
          <span key={def.key} className={slotIsFilled(slots, def.key) ? 'is-filled' : ''}>
            {def.label}
            {slotIsFilled(slots, def.key) && <> · {slotDisplayValue(slots, def.key)}</>}
          </span>
        ))}
      </div>
    </div>
  )
}
