import { useEffect, useRef } from 'react'
import { Icon } from '../shared/Icon'
import { formatSessionTime } from './intakeHistory'
import type { IntakeSessionRecord } from './intakeHistory'

/** 历史对话与草稿管理模态窗：
 * 展示已暂存的未完成规划对话，支持一键恢复继续、单条删除与全部清空。
 * FEUX-6：aria-modal 声明了模态语义就得兑现——打开即移焦入弹窗、Esc 关闭、
 * Tab 在弹窗内循环（不穿透到背景页）。 */
export function IntakeSessionsModal({
  sessions,
  open,
  onClose,
  onResume,
  onDelete,
  onClearAll,
}: {
  sessions: IntakeSessionRecord[]
  open: boolean
  onClose: () => void
  onResume: (session: IntakeSessionRecord) => void
  onDelete: (id: string) => void
  onClearAll: () => void
}) {
  const modalRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const modal = modalRef.current
    // 打开即移焦（读屏用户第一时间知道焦点去了哪），找不到可聚焦元素时落在容器上
    const focusables = modal?.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    ;(focusables && focusables.length ? focusables[0] : modal)?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab' || !modal) return
      const items = Array.from(
        modal.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
      ).filter((el) => el.offsetParent !== null)
      if (!items.length) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (event.shiftKey && (active === first || !modal.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !modal.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="intake-sessions-overlay"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="规划对话管理"
    >
      <div className="intake-sessions-modal" ref={modalRef} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="intake-sessions-head">
          <div className="intake-sessions-title">
            <Icon name="clock" size={18} />
            <h3>历史规划对话</h3>
            <span className="intake-sessions-count">{sessions.length} 条草稿</span>
          </div>
          <button className="text-action" type="button" onClick={onClose} aria-label="关闭">
            <Icon name="close" size={16} />
          </button>
        </div>

        <div className="intake-sessions-body">
          {sessions.length === 0 ? (
            <p className="intake-sessions-empty">暂无未完成的对话草稿</p>
          ) : (
            <div className="intake-sessions-list">
              {sessions.map((item) => (
                <div key={item.id} className="intake-session-item">
                  <div className="intake-session-main">
                    <strong className="intake-session-name">{item.title}</strong>
                    <div className="intake-session-meta">
                      <span className="intake-session-time">{formatSessionTime(item.updatedAt)}</span>
                      {item.slots.city && <span className="intake-session-tag">{item.slots.city}</span>}
                      {item.slots.days && <span className="intake-session-tag">{item.slots.days}天</span>}
                      {item.slots.persons && <span className="intake-session-tag">{item.slots.persons}人</span>}
                    </div>
                  </div>
                  <div className="intake-session-actions">
                    <button
                      type="button"
                      className="button button-primary intake-session-resume"
                      onClick={() => onResume(item)}
                    >
                      继续对话
                    </button>
                    <button
                      type="button"
                      className="text-action intake-session-del"
                      title="删除此草稿"
                      onClick={() => onDelete(item.id)}
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {sessions.length > 0 && (
          <div className="intake-sessions-foot">
            <button type="button" className="text-action intake-sessions-clear" onClick={onClearAll}>
              清空全部记录
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
