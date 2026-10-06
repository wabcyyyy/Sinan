import { lazy, Suspense, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { getUserInfo, logout } from '../api/sinan'
import { AppShell } from './layout/AppShell'
import { ExplorePage } from './explore/ExplorePage'
import { GuideDetailPage } from './explore/GuideDetailPage'
import { HomePage } from './home/HomePage'
import { LoginPage } from './auth/LoginPage'
import { TripsPage } from './itinerary/TripsPage'
import { SettingsPage } from './settings/SettingsPage'
import { navigate, useLocation } from './router'
import { LoadingBlock, NotFoundBlock } from './shared/States'
import { SharePage } from './shared/SharePage'

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
  if (path === '/login') return '登录 · 司南 Sinan'
  return '司南 Sinan'
}

export default function App() {
  const location = useLocation()
  const [username, setUsername] = useState(() => localStorage.getItem('sinan-username') || '')
  useEffect(() => { document.title = pageTitle(location.path) }, [location.path])
  // 用户名以服务端为准：localStorage 只是首帧占位，启动后对账 /user/info，
  // 防止换账号/登出后导航仍显示上一个用户（401 时连占位一起清掉）。
  useEffect(() => {
    let cancelled = false
    getUserInfo().then(
      (info) => {
        if (cancelled) return
        if (info?.username) {
          setUsername(info.username)
          localStorage.setItem('sinan-username', info.username)
        }
      },
      () => {
        if (cancelled) return
        setUsername('')
        localStorage.removeItem('sinan-username')
      },
    )
    return () => { cancelled = true }
  }, [])
  const authenticated = (value: string) => { setUsername(value); localStorage.setItem('sinan-username', value) }
  /** 登出（P1-7）：先清本地身份并回首页；POST /auth/logout 清 HttpOnly Cookie 是 best-effort，失败不阻塞。 */
  const handleLogout = () => {
    setUsername('')
    localStorage.removeItem('sinan-username')
    logout().catch(() => {})
    navigate('/')
  }
  if (location.path === '/login') return <LoginPage onAuthenticated={authenticated} />
  if (location.path.startsWith('/s/')) return <SharePage path={location.path} />
  let page: ReactNode
  if (location.path === '/') page = <HomePage />
  else if (location.path === '/explore' || location.path === '/explore/') page = <ExplorePage />
  else if (location.path.startsWith('/explore/guide/')) page = <GuideDetailPage path={location.path} />
  else if (location.path === '/trips') page = <TripsPage />
  else if (location.path === '/settings') page = <SettingsPage />
  else if (location.path.startsWith('/trips/')) page = <TripDetailPage path={location.path} />
  else page = <NotFoundBlock onHome={() => navigate('/')} />
  return (
    <AppShell onLogin={() => navigate('/login')} username={username} onLogout={handleLogout}>
      <Suspense fallback={<LoadingBlock label="正在打开行程…" />}>{page}</Suspense>
      {username && <span className="sr-only">已登录：{username}</span>}
    </AppShell>
  )
}
