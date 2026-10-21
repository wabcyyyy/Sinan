import { useEffect, useRef, useState } from 'react'
import type { TemplatePublishVO } from '../../types/generated/contracts'
import type { ItineraryDetail } from '../../types/itinerary'
import { previewPublicItinerary, publishPublicItinerary, unpublishPublicItinerary } from '../../api/sinan'
import { isDemoMode } from '../../demoMode'
import { navigate } from '../router'
import './templates.css'

export function PublishItineraryPanel({ trip }: { trip: ItineraryDetail }) {
  const [open, setOpen] = useState(false)
  const [preview, setPreview] = useState<TemplatePublishVO | null>(null)
  const [published, setPublished] = useState(Boolean(trip.templatePublishedAt))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const lock = useRef(false)
  useEffect(() => { setOpen(false); setPreview(null); setPublished(Boolean(trip.templatePublishedAt)) }, [trip])
  if (trip.myRole && trip.myRole !== 'owner') return null
  const run = async (action: 'preview' | 'publish' | 'unpublish') => {
    if (lock.current) return
    setOpen(true)
    setError('')
    if (isDemoMode()) { setError('演示版可以复制和微调参考路线；公开发布需要连接真实服务。'); return }
    lock.current = true
    setBusy(true)
    try {
      if (action === 'unpublish') { await unpublishPublicItinerary(trip.id); setPublished(false); setPreview(null) }
      else {
        const result = action === 'publish' ? await publishPublicItinerary(trip.id) : await previewPublicItinerary(trip.id)
        setPreview(result)
        setPublished(Boolean(result.publishedAt))
      }
    } catch (err) { setError(err instanceof Error ? err.message : '操作未完成，请稍后重试') }
    finally { lock.current = false; setBusy(false) }
  }
  return <section className="public-publish" aria-label="公开行程管理"><div className="public-publish-head"><div><strong>{published ? '这份路线已在行程库公开' : '把这份路线，分享给下一位旅行者'}</strong><p>公开一份路线快照；你的后续编辑不会自动发布。</p></div><button className="button button-secondary" type="button" disabled={busy || trip.status !== 2} onClick={() => open ? setOpen(false) : void run('preview')}>{open ? '收起' : published ? '管理公开规划' : '预览并发布'}</button></div>
    {open && <><p>公开内容包含标题、人数、路线、封面和费用档位。不会公开你的日期、精确花费、私人备注、聊天、账目或预订。请确认标题、地点与封面本身没有个人隐私。</p>{preview && <div className="public-publish-preview"><strong>{preview.summary.title}</strong>{preview.summary.dayList.map((day) => <p key={day.dayNo}>第 {day.dayNo} 天 · {day.items.map((item) => item.poiName).join(' → ')}</p>)}</div>}{error && <p role="alert">{error}</p>}<div className="public-publish-actions">{preview && <button className="button button-primary" type="button" disabled={busy} onClick={() => void run('publish')}>{busy ? '处理中…' : published ? '用当前路线更新公开版本' : '确认公开发布'}</button>}{published && <><button className="button button-secondary" type="button" onClick={() => navigate(`/itineraries/${trip.id}`)}>查看公开页面</button><button className="button button-secondary" type="button" disabled={busy} onClick={() => void run('unpublish')}>下架公开规划</button></>}</div></>}
  </section>
}
