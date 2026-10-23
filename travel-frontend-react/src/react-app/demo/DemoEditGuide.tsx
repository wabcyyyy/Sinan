import { useState } from 'react'

export function DemoEditGuide({ busy, hasDraft, saved, onDay, onEdit, onReview, onClose }: {
  busy: boolean
  hasDraft: boolean
  saved: boolean
  onDay: () => void
  onEdit: () => void
  onReview: () => void
  onClose: () => void
}) {
  const [step, setStep] = useState(0)
  const current = saved ? 3 : hasDraft ? 2 : step
  return <aside className="demo-edit-guide" aria-label="局部修改体验引导">
    <div><span className="workbench-eyebrow">约 90 秒 · 试一次局部调整</span>
      <strong>{['① 看看原来的两日安排', '② 把第二天安排松一点', '③ 核对差异，确认后保存', '体验完成，修改已保存'][current]}</strong>
      <p>{['时间线与地图对应同一份行程。先看看第二天，再试一次局部修改。', '只改第二天，保留第一天和住宿。试着给午后多留一点空闲。', '移除项会标在原时间线上，修改预览展示调整后安排。确认前，原行程保持原样。', '现在可以切换日期或刷新页面，看看保存后的安排。'][current]}</p>
    </div>
    <div className="demo-edit-guide-actions">
      {current === 0 && <button className="button button-primary" onClick={() => { onDay(); setStep(1) }}>查看第二天</button>}
      {current === 1 && <button className="button button-primary" disabled={busy} onClick={onEdit}>{busy ? '正在准备建议…' : '试试这句调整'}</button>}
      {current === 2 && <button className="button button-primary" onClick={onReview}>查看修改预览</button>}
      <button className="text-action" onClick={onClose}>{current === 3 ? '结束引导' : '跳过引导'}</button>
    </div>
  </aside>
}
