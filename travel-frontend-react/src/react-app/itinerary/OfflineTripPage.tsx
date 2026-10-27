import { useEffect, useState } from 'react'
import { directionsLink } from './mapPins'
import { expired, getOfflineOwner, listOfflineTrips, readOfflineTrip, removeOfflineTrip, type OfflineTrip } from './offlineTrip'
import { destinationDate } from './TravelToday'
import { navigate } from '../router'
import './offline.css'

export function OfflineTripPage({ path }: { path: string }) {
  const id = Number(path.match(/^\/offline\/(\d+)$/)?.[1])
  const owner = getOfflineOwner()
  const [packs, setPacks] = useState<OfflineTrip[]>([])
  const [pack, setPack] = useState<OfflineTrip | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [dayNo, setDayNo] = useState(1)
  const timeZone = pack?.timeZone || 'Asia/Shanghai'
  useEffect(() => {
    setLoaded(false); setPack(null); setPacks([]); setError('')
    if (!owner) return
    let active = true
    const query = Number.isInteger(id) && id > 0 ? readOfflineTrip(owner.id, id).then(value => { if (active) setPack(value) })
      : listOfflineTrips(owner.id).then(value => { if (active) setPacks(value) })
    query.catch(err => { if (active) setError(err instanceof Error ? err.message : '离线资料读取失败') })
      .finally(() => { if (active) setLoaded(true) })
    return () => { active = false }
  }, [owner?.id, id])
  useEffect(() => { if (pack) setDayNo(pack.days.find(d => d.travelDate === destinationDate(timeZone))?.dayNo ?? 1) }, [pack, timeZone])
  if (!owner) return <main className="offline-page"><h1>尚未解锁本机离线行程</h1><p>请先在联网状态下登录本人账号，再主动保存真实行程。退出登录会清除本机身份标记。</p><a href="/">返回司南</a></main>
  if (error) return <main className="offline-page"><h1>读取失败</h1><p role="alert">{error}</p></main>
  if (!id) return <main className="offline-page"><h1>本机离线行程</h1><p>当前账号：{owner.username} · 只读取此设备上主动保存的资料</p>{!loaded ? <p role="status">正在读取本机资料…</p> : packs.length ? <ul>{packs.map(p => <li key={p.key}><a href={`/offline/${p.tripId}`}>{p.title}</a><small>{expired(p) ? '已过期 · 请联网重新保存' : `保存于 ${new Date(p.savedAt).toLocaleString('zh-CN')}`}</small></li>)}</ul> : <p>这个账号还没有主动保存的行程。</p>}<a href="/trips">联网后打开我的行程</a></main>
  if (!pack) return <main className="offline-page" role="status">{loaded ? '这份行程没有保存在当前账号的本机副本中。' : '正在读取本人保存的离线行程…'}<p><a href="/offline">返回本机行程</a></p></main>
  const day = pack.days.find(d => d.dayNo === dayNo) || pack.days[0]
  const now = new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date())
  const nextIndex = day.items.findIndex(p => p.id && !pack.completedItemIds.includes(p.id) && (day.travelDate !== destinationDate(timeZone) || !p.endTime || p.endTime.slice(0, 5) >= now))
  const next = nextIndex >= 0 ? day.items[nextIndex] : null
  const prev = nextIndex > 0 ? day.items[nextIndex - 1] : undefined
  const changeDate = day.travelDate === destinationDate(timeZone)
  return <main className="offline-page"><header><a href="/offline">← 本机行程</a><small>离线只读 · {pack.ownerName} · 修订 {pack.revision}</small><h1>{pack.title}</h1><p>保存于 {new Date(pack.savedAt).toLocaleString('zh-CN')} · {expired(pack) ? '已过期，资料可能过时，请联网重新保存' : `有效期参考至 ${new Date(pack.expiresAt).toLocaleDateString('zh-CN')}`}</p><p>资料来自保存时的真实行程；后续在线修改不会自动同步到这里。</p></header>
    <p>当前时间按目的地 {timeZone}。</p><nav aria-label="选择日期">{pack.days.map(d => <button key={d.dayNo} aria-pressed={d.dayNo === dayNo} onClick={() => setDayNo(d.dayNo)}>{d.travelDate === destinationDate(timeZone) ? '今天' : `第 ${d.dayNo} 天`}</button>)}</nav>
    <h2>{changeDate ? '今天' : `第 ${day.dayNo} 天`} · {day.theme}</h2>
    {next && <section className="offline-next"><small>下一站 · {next.startTime?.slice(0, 5)}</small><h3>{next.poiName}</h3><p>{next.address || next.remark}</p><a href={directionsLink(pack.city, next, prev)} target="_blank" rel="noreferrer">{prev ? '查看两点导航' : '导航到这一站'} ↗</a><p>外部导航需要联网，先核对匹配的地点。</p>{pack.bookings.filter(b => b.itemId === next.id).map(b => <details key={b.id}><summary>已订资料 · {b.name}</summary><p>确认号 {b.reference || '未填写'}</p><p>{b.note}</p></details>)}</section>}
    <ol className="offline-schedule">{day.items.map((item, index) => <li key={item.id || index}><time>{item.startTime?.slice(0, 5) || '--:--'}</time><div><strong>{item.poiName}</strong><p>{item.address || item.remark}</p></div>{item.id && pack.completedItemIds.includes(item.id) && <small>保存前已完成</small>}</li>)}</ol>
    <details><summary>已订资料与确认号</summary>{pack.bookings.map(b => <p key={b.id}>{b.name} · {b.startAt?.replace('T', ' ')} · {b.reference || '未填确认号'}<br />{b.address}<br />{b.note}</p>)}</details>
    <details><summary>行前清单</summary>{pack.tasks.map(t => <p key={t.id}>{t.done ? '✓' : '○'} {t.title}</p>)}</details>
    <details><summary>用户核实记录</summary>{pack.confirmations.map(c => <p key={c.id}>{c.name} · {c.note} · {c.recordedAt || '时间未知'}</p>)}</details>
    <button type="button" className="offline-delete" onClick={async () => { if (!window.confirm('从这台设备移除这份离线资料？在线行程不会删除。')) return; await removeOfflineTrip(owner.id, pack.tripId); navigate('/offline') }}>移除本机副本</button>
  </main>
}
