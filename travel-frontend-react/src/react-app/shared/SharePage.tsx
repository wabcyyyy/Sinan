import { useEffect, useState } from 'react'
import { getSharedItinerary } from '../../api/sinan'
import { navigate, routeId } from '../router'
import { BrandMark } from './Brand'
import { Icon } from './Icon'
import { ErrorBlock, LoadingBlock } from './States'

type SharedData = Awaited<ReturnType<typeof getSharedItinerary>>

export function SharePage({ path }: { path: string }) {
  const token = routeId(path, '/s/') || ''
  const [data, setData] = useState<SharedData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  useEffect(() => {
    if (!token) { setLoading(false); setError(true); return }
    getSharedItinerary(token).then((result) => { setData(result); document.title = `${result.title} · 司南 Sinan` }).catch(() => setError(true)).finally(() => setLoading(false))
    return () => { document.title = '司南 Sinan' }
  }, [token])
  return <div className="share-page-react"><header className="share-react-top"><button className="sinan-brand" type="button" onClick={() => navigate('/')}><BrandMark /><span className="sinan-brand-copy"><strong>司南</strong><small>Sinan</small></span></button><button className="button button-secondary" type="button" onClick={() => navigate('/')}>我也来做一份<Icon name="arrow" size={15} /></button></header><main className="share-react-main">{loading ? <LoadingBlock label="正在打开分享行程…" /> : error || !data ? <ErrorBlock message="链接无效或已失效" onRetry={() => window.location.reload()} /> : <><section className="share-react-hero"><span className="section-eyebrow">{data.city}</span><h1>{data.title}</h1><p>{data.days} 天 · {data.persons} 人{data.startDate ? ` · ${data.startDate} — ${data.endDate || ''}` : ''}{data.budget != null ? ` · 预算 ￥${data.budget}` : ''}</p>{data.tripTheme && <strong>{data.tripTheme}</strong>}</section>{data.dayList.map((day) => <section className="share-day" key={day.dayNo}><header><span>DAY {String(day.dayNo).padStart(2, '0')}</span><h2>{day.theme || `第 ${day.dayNo} 天`}</h2><b>￥{day.dayTotalAmount}</b></header>{day.note && <p className="share-day-note">{day.note}</p>}<ul>{day.items.map((item, index) => <li key={`${day.dayNo}-${index}`}><span className="share-item-time">{item.startTime || '--:--'}{item.endTime ? ` — ${item.endTime}` : ''}</span><span><strong>{item.poiName}</strong><small>{item.address || '暂无地址'} · {item.itemType}</small></span>{item.cost != null && <b>￥{item.cost}</b>}</li>)}</ul></section>)}{data.budgetList.length > 0 && <section className="share-day share-budget"><header><span>SUMMARY</span><h2>预算参考</h2><b>￥{data.totalAmount}</b></header>{data.budgetList.map((row) => <div className="share-budget-row" key={row.category}><span>{row.category}</span><b>￥{row.amount ?? 0}</b></div>)}</section>}<p className="share-note">公开分享只读 · 行程事实可能变化，请在出发前再次核实。</p></>}</main><footer className="share-react-footer"><button className="button button-primary" type="button" onClick={() => navigate('/')}>生成我的行程<Icon name="arrow" size={16} /></button><p>司南 Sinan · 为每一段旅程找到方向</p></footer></div>
}
