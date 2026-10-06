/**
 * 条目反馈控制（C3.5）：「没问题 / 有误」两态切换 + 有误原因选择器。
 *
 * 数据与 API 归父层（TripDetailPage 持有本人反馈索引）；本组件只做交互与口径
 * 组装（buildFeedbackPayload）。点击已激活态 = 撤销（后端 upsert 一人一条 + 硬删
 * 撤销的语义映射）；切换到另一态直接覆盖，无需先撤销。
 * 文件名带 Control 后缀：case-insensitive 文件系统上 `./ItemFeedback` 会被 Vite 的
 * .ts 先于 .tsx 解析序先命中 itemFeedback.ts（预演实证）。
 */
import { useState } from 'react'
import type * as Contracts from '../../types/generated/contracts'
import { Icon } from '../shared/Icon'
import { buildFeedbackPayload, FEEDBACK_REASON_LABELS, FEEDBACK_REASON_ORDER, type FeedbackReason } from './itemFeedback'

interface ItemFeedbackProps {
  itemId: number
  feedback: Contracts.FeedbackVO | undefined
  onSet: (payload: Contracts.FeedbackCreate) => Promise<void>
  onRevoke: () => Promise<void>
}

export function ItemFeedbackControl({ itemId, feedback, onSet, onRevoke }: ItemFeedbackProps) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [reason, setReason] = useState<FeedbackReason | null>(null)
  const [note, setNote] = useState('')
  const [pending, setPending] = useState(false)

  const current = feedback?.value
  const run = async (action: () => Promise<void>) => {
    if (pending) return
    setPending(true)
    try {
      await action()
    } catch {
      /* 父层统一 toast；本组件保持当前交互态供重试 */
    } finally {
      setPending(false)
    }
  }
  const markRight = () => run(() => (current === 'right' ? onRevoke() : onSet(buildFeedbackPayload(itemId, 'right'))))
  const markWrong = () => run(async () => { if (current === 'wrong') await onRevoke(); else setPickerOpen(true) })
  const submitWrong = () => {
    const payload = reason ? buildFeedbackPayload(itemId, 'wrong', reason, note) : null
    if (!payload) return
    run(async () => {
      await onSet(payload)
      setPickerOpen(false)
      setReason(null)
      setNote('')
    })
  }

  return <div className="item-feedback">
    <button className={current === 'right' ? 'fb-toggle active' : 'fb-toggle'} type="button" aria-pressed={current === 'right'} disabled={pending} onClick={markRight} title="这条安排没问题">
      <Icon name="check" size={13} />没问题
    </button>
    <button className={current === 'wrong' ? 'fb-toggle active' : 'fb-toggle'} type="button" aria-pressed={current === 'wrong'} disabled={pending} onClick={markWrong} title="这条安排与实际不符">
      <Icon name="close" size={13} />有误
    </button>
    {pickerOpen && <div className="fb-picker">
      <span className="fb-picker-title">哪里不对？（帮我们修正这条安排）</span>
      <div className="fb-reasons">{FEEDBACK_REASON_ORDER.map((key) => <button key={key} className={reason === key ? 'fb-reason active' : 'fb-reason'} type="button" aria-pressed={reason === key} onClick={() => setReason(reason === key ? null : key)}>{FEEDBACK_REASON_LABELS[key]}</button>)}</div>
      <textarea className="fb-note" value={note} maxLength={200} rows={2} placeholder="补充说明（可选，200 字内）" onChange={(event) => setNote(event.target.value)} />
      <div className="fb-actions">
        <button className="button button-primary" type="button" disabled={!reason || pending} onClick={submitWrong}>提交</button>
        <button className="button button-secondary" type="button" disabled={pending} onClick={() => { setPickerOpen(false); setReason(null); setNote('') }}>取消</button>
      </div>
    </div>}
  </div>
}
