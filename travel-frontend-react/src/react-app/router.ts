import { intakeSessionStorage } from './home/intakeStorage'
import { useCallback, useEffect, useState } from 'react'
import { appHref, appPath } from '../demoMode'

export interface LocationState {
  path: string
  query: URLSearchParams
}

// 探索页三合一（2026-10-02，PLAN §1.1）：旧三页路径 → /explore 的 replace 重定向表，
// 直达/刷新/后退都落到新地址，不留死链。导出仅供测试（表驱动用例）。
export function legacyExploreRedirect(pathname: string): string | null {
  if (pathname === '/destinations') return '/explore?tab=destinations'
  if (pathname === '/inspiration') return '/explore?tab=inspiration'
  if (pathname === '/guides' || pathname === '/guides/') return '/explore?tab=guides'
  if (pathname.startsWith('/guides/')) {
    const slug = pathname.slice('/guides/'.length).split('/').filter(Boolean)[0]
    if (slug) return `/explore/guide/${slug}`
  }
  return null
}

function readLocation(): LocationState {
  const redirected = legacyExploreRedirect(appPath(window.location.pathname))
  if (redirected) window.history.replaceState({}, '', appHref(redirected))
  // Retired creation pages share the homepage form; preserve bookmarked input.
  if (['/plan', '/generate'].includes(appPath(window.location.pathname))) {
    window.history.replaceState({}, '', appHref(`/${window.location.search}`))
  }
  return { path: appPath(window.location.pathname), query: new URLSearchParams(window.location.search) }
}

export function navigate(to: string) {
  to = appHref(to)
  if (to === window.location.pathname + window.location.search) return
  window.history.pushState({}, '', to)
  window.dispatchEvent(new PopStateEvent('popstate'))
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

/** 重置首页到最初的 idle 状态（清空临时对话与规划草稿，派发 reset 事件） */
export function resetHomeSession() {
  try {
    intakeSessionStorage.removeItem('current')
    intakeSessionStorage.removeItem('generation')
  } catch {
    // ignore
  }
  window.dispatchEvent(new CustomEvent('sinan:reset-home'))
}

export function loginRedirect(path = `${window.location.pathname}${window.location.search}`) {
  return `/login?redirect=${encodeURIComponent(path || '/')}`
}

export function useLocation() {
  const [location, setLocation] = useState<LocationState>(() => readLocation())
  useEffect(() => {
    const onPopState = () => setLocation(readLocation())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])
  const go = useCallback((to: string) => navigate(to), [])
  return { ...location, navigate: go }
}

export function routeId(path: string, prefix: string): string | null {
  if (!path.startsWith(prefix)) return null
  const rest = path.slice(prefix.length).split('/').filter(Boolean)
  return rest[0] || null
}
