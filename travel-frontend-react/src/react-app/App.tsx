import { isDemoMode } from '../demoMode'
import { DemoBanner } from './demo/DemoBanner'
import type { UserInfoVO } from '../types/generated/contracts'
import { activateIntakeOwner, clearLegacyIntakeStorage, suspendIntakeOwner } from './home/intakeStorage'
import { lazy, Suspense, useEffect, useState, useRef } from 'react'
import type { ReactNode } from 'react'
import { getUserInfo, logout, isUnauthorized, cancelSessionRequests } from '../api/sinan'
import { AppShell } from './layout/AppShell'
import { ExplorePage } from './explore/ExplorePage'
import { GuideDetailPage } from './explore/GuideDetailPage'
import { HomePage } from './home/HomePage'
import { LoginPage } from './auth/LoginPage'
import { TripsPage } from './itinerary/TripsPage'
import { SettingsPage } from './settings/SettingsPage'
import { navigate, useLocation } from './router'
import { ErrorBlock, LoadingBlock, NotFoundBlock, ScreenState } from './shared/States'
import { SharePage } from './shared/SharePage'
import { PrivacyPage } from './shared/PrivacyPage'
import { PublicItineraries, PublicItineraryDetail } from './templates/PublicItineraries'
import '../styles/trial.css'
const AdminPage = lazy(() => import('./admin/AdminPage').then((module) => ({ default: module.AdminPage })))

// 行程详情页是 maplibre-gl 的唯一消费链（全站最重依赖），路由级懒加载让
// 登录/首页/列表的首屏不必先下载地图库；chunk 拉取失败会作为渲染错误
// 冒泡到根 ErrorBoundary（重试=整页刷新）。
const TripDetailPage = lazy(() =>
  import('./itinerary/TripDetailPage').then((module) => ({ default: module.TripDetailPage })),
)

function pageTitle(path: string) {
  if (path === '/') return '司南 Sinan · 让每一段旅程找到方向'
  if (path === '/explore' || path.startsWith('/explore/')) return '探索 · 司南 Sinan'
  if (path === '/trips' || path.startsWith('/trips/')) return '我的行程 · 司南 Sinan'
  if (path === '/settings') return '设置 · 司南 Sinan'
  if (path.startsWith('/itineraries')) return '行程库 · 司南 Sinan'
  if (path === '/login') return '登录 · 司南 Sinan'
  return '司南 Sinan'
}

export default function App() {
  const location = useLocation()
  useEffect(() => { document.title = pageTitle(location.path) }, [location.path])
  return <BusinessApp />
}

function BusinessApp() {
  const location = useLocation()
  const revision = useRef(0)
  const demo = isDemoMode()
  const [user, setUser] = useState<UserInfoVO | null>(() => {
    if (!demo) return null
    activateIntakeOwner(0)
    return { id: 0, username: '演示访客', nickname: '演示访客', phone: null, role: 'user' }
  })
  const [status, setStatus] = useState<'checking' | 'ready' | 'logout' | 'logout-error' | 'check-error'>(demo ? 'ready' : 'checking')
  const [sessionVersion, setSessionVersion] = useState(0)
  useEffect(() => { document.title = pageTitle(location.path) }, [location.path])
  useEffect(() => {
    if (demo) return
    let disposed = false
    try { clearLegacyIntakeStorage() } catch { /* unavailable storage */ }
    const verify = async () => {
      const mine = ++revision.current
      cancelSessionRequests()
      suspendIntakeOwner()
      setStatus('checking')
      try {
        const info = await getUserInfo()
        if (disposed || mine !== revision.current) return
        activateIntakeOwner(info.id)
        setUser(info)
        setStatus('ready')
        setSessionVersion((value) => value + 1)
      } catch (error) {
        if (disposed || mine !== revision.current) return
        if (isUnauthorized(error)) {
          activateIntakeOwner(null)
          setUser(null)
          try { localStorage.removeItem('sinan-username') } catch { /* unavailable storage */ }
          setStatus('ready')
          setSessionVersion((value) => value + 1)
        } else setStatus('check-error')
      }
    }
    const changed = (event: StorageEvent) => { if (event.key === 'sinan-auth-change') void verify() }
    window.addEventListener('storage', changed)
    void verify()
    return () => { disposed = true; revision.current += 1; window.removeEventListener('storage', changed) }
  }, [])
  const authenticated = (info: UserInfoVO) => {
    revision.current += 1
    cancelSessionRequests()
    activateIntakeOwner(info.id, true)
    setUser(info)
    setStatus('ready')
    setSessionVersion((value) => value + 1)
    broadcastIdentity(info.username)
  }
  const handleLogout = async () => {
    const mine = ++revision.current
    cancelSessionRequests()
    suspendIntakeOwner()
    setStatus('logout')
    try {
      await logout()
      if (mine !== revision.current) return
      activateIntakeOwner(null)
      setUser(null)
      broadcastIdentity('')
      setSessionVersion((value) => value + 1)
      setStatus('ready')
      navigate('/')
    } catch { if (mine === revision.current) setStatus('logout-error') }
  }
  if (status === 'checking' || status === 'logout') return <ScreenState><LoadingBlock label={status === 'logout' ? '正在退出登录…' : '正在确认登录状态…'} /></ScreenState>
  if (status === 'logout-error') return <ScreenState><ErrorBlock title="退出未完成" message="网络可能断开了，稍后再试一次。" retryLabel="重试退出" onRetry={() => void handleLogout()} /></ScreenState>
  if (status === 'check-error') return <ScreenState><ErrorBlock title="暂时没有连上司南" message="无法确认登录状态，检查网络后再试一次。" retryLabel="重试" onRetry={() => window.location.reload()} /></ScreenState>
  const username = user?.username ?? ''
  if (location.path === '/login' && !demo) return <LoginPage onAuthenticated={authenticated} />
  if (location.path.startsWith('/s/')) return <SharePage path={location.path} signedIn={Boolean(username)} />
  let page: ReactNode
  if (location.path === '/') page = <HomePage signedIn={Boolean(username)} />
  else if (location.path === '/itineraries') page = <PublicItineraries />
  else if (/^\/itineraries\/-?\d+$/.test(location.path)) page = <PublicItineraryDetail key={location.path} id={Number(location.path.split('/').pop())} signedIn={Boolean(username)} />
  else if (location.path === '/admin') page = <AdminPage />
  else if (location.path === '/privacy') page = <PrivacyPage />
  else if (location.path === '/explore' || location.path === '/explore/') page = <ExplorePage />
  else if (location.path.startsWith('/explore/guide/')) page = <GuideDetailPage path={location.path} />
  else if (location.path === '/trips') page = <TripsPage />
  else if (location.path === '/settings') page = <SettingsPage />
  else if (location.path.startsWith('/trips/')) page = <TripDetailPage key={location.path} path={location.path} />
  else page = <div className="page-content"><NotFoundBlock onHome={() => navigate('/')} /></div>
  return (
    <AppShell onLogin={() => navigate('/login')} username={username} onLogout={handleLogout}>
      {demo && <DemoBanner />}
      <Suspense key={`${user?.id ?? 'anonymous'}:${sessionVersion}`} fallback={<LoadingBlock label="正在打开行程…" />}>{page}</Suspense>
      {username && <span className="sr-only">{demo ? '当前身份：' : '已登录：'}{username}</span>}
    </AppShell>
  )
}

function broadcastIdentity(username: string): void {
  try {
    if (username) localStorage.setItem('sinan-username', username)
    else localStorage.removeItem('sinan-username')
    localStorage.setItem('sinan-auth-change', crypto.randomUUID())
  } catch { /* storage availability does not change server authentication */ }
}
