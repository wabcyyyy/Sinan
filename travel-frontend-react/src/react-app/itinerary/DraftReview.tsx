import type { ChatDayPlan } from '../../types/chat'
import type { DraftPreview } from './draftPreview'
import { itemPreviewMark } from './draftPreview'
import { describeStructuredChange } from './chatDraft'

export function DraftReview({ preview, plans, dayNo, persons, applying, onApply, onDismiss }: {
  preview: DraftPreview
  plans: ChatDayPlan[]
  dayNo: number
  persons: number
  applying: boolean
  onApply: () => void
  onDismiss: () => void
}) {
  const day = plans.find(item => item.day_no === dayNo)
  return <section className="draft-review" aria-label="局部修改预览">
    <div className="draft-review-head"><span className="workbench-eyebrow">调整后 · 尚未保存</span><h2>先看变化，再决定</h2>
      <p>当前行程尚未改变。核对原安排与下方提案，再确认保存；地图仍显示当前安排。</p></div>
    <div className="draft-review-stats">
      {preview.removed > 0 && <span>移除 {preview.removed} 项</span>}
      {preview.added > 0 && <span>新增 {preview.added} 项</span>}
      <span>{preview.costDelta == null ? '费用变化待核对' : `条目费用估算 ${preview.costDelta > 0 ? '+' : preview.costDelta < 0 ? '−' : ''}￥${Math.abs(preview.costDelta).toLocaleString('zh-CN')}`}</span>
    </div>
    {preview.costDelta != null && <p className="draft-review-footnote">按 {persons} 人比较已列条目，不含未列出的费用；最终以保存后的费用小计为准。</p>}
    {(preview.untouchedDays.length > 0 || preview.hotelsUntouched) && <p className="draft-review-preserved">
      {preview.untouchedDays.length > 0 && `第 ${preview.untouchedDays.join('、')} 天不在本次修改范围`}
      {preview.hotelsUntouched && ' · 住宿也未涉及'}
    </p>}
    <ul className="draft-review-changes">{preview.changes.map((change, index) => <li key={index}>{describeStructuredChange(change)}</li>)}</ul>
    <h3>第 {dayNo} 天 · 调整后</h3>
    {day ? <ol className="draft-review-items">{day.items?.map((item, index) => {
      const mark = itemPreviewMark(preview.changes, dayNo, item.poi_name ?? '', true)
      return <li key={index} className={mark ? `preview-${mark}` : ''}>
        <span>{item.start_time || '时间待定'}{item.end_time && `–${item.end_time}`}</span>
        <strong>{item.poi_name || '待确认地点'}</strong>
        {mark && <small>{mark === 'added' ? '将新增' : mark === 'moved' ? '将移入' : '将调整'}</small>}
      </li>
    })}</ol> : <p>这一天未包含在建议中，当前安排保留。</p>}
    <p className="draft-review-footnote">新增地点、价格与开放信息仍需出发前核实。</p>
    <div className="draft-review-actions">
      <button type="button" className="button button-primary" disabled={applying} onClick={onApply}>{applying ? '正在保存…' : '应用到行程'}</button>
      <button type="button" className="button button-secondary" disabled={applying} onClick={onDismiss}>暂不应用</button>
    </div>
  </section>
}
