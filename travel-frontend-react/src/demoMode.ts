/** 展示构建与 /demo 入口共用正式页面；身份和草稿与真实账号隔离。 */
export function isDemoMode(): boolean {
  return import.meta.env.VITE_DEMO_MODE === 'true'
    || (typeof window !== 'undefined' && /^\/demo(?:\/|$)/.test(window.location.pathname))
}

export function appHref(path: string): string {
  const prefix = import.meta.env.VITE_DEMO_MODE !== 'true' && isDemoMode() ? '/demo' : ''
  return prefix && path.startsWith('/') && !path.startsWith('/demo') ? `${prefix}${path}` : path
}

export function appPath(path: string): string {
  return path.replace(/^\/demo(?=\/|$)/, '') || '/'
}

export function storageKey(key: string): string {
  return isDemoMode() ? `sinan-demo:${key}` : key
}

export function hasIdentity(): boolean {
  return isDemoMode() || Boolean(localStorage.getItem('sinan-username'))
}

export function resetDemo(): void {
  for (const storage of [sessionStorage, localStorage]) {
    for (const key of Object.keys(storage)) if (key.startsWith('sinan-demo:')) storage.removeItem(key)
  }
  window.location.assign(appHref('/'))
}
