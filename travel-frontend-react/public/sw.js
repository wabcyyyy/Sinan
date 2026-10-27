// 只缓存公开应用壳与静态资源；用户私有行程存在 IndexedDB，API 响应绝不进入 CacheStorage。
const SHELL = 'sinan-offline-shell-v1'
self.addEventListener('install', event => { event.waitUntil(self.skipWaiting()) })
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) if (name.startsWith('sinan-offline-shell-') && name !== SHELL) await caches.delete(name)
    await self.clients.claim()
  })())
})
self.addEventListener('message', event => {
  if (event.data?.type !== 'CACHE_OFFLINE_SHELL') return
  event.waitUntil((async () => {
    try {
      const cache = await caches.open(SHELL)
      const assets = ['/', '/offline', ...event.data.assets].filter(path =>
        typeof path === 'string' && path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/api/'))
      for (const path of new Set(assets)) {
        const response = await fetch(path, { credentials: 'same-origin' })
        if (!response.ok || response.type === 'opaque') throw new Error('静态页面资源未能保存')
        await cache.put(path, response)
      }
      event.ports[0]?.postMessage({ ok: true })
    } catch (error) { event.ports[0]?.postMessage({ ok: false, error: error instanceof Error ? error.message : '保存失败' }) }
  })())
})
self.addEventListener('fetch', event => {
  const request = event.request
  const url = new URL(request.url)
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return
  if (request.mode === 'navigate') {
    if (!url.pathname.startsWith('/offline')) return
    event.respondWith(fetch(request).catch(async () => (await caches.open(SHELL)).match('/offline') || Response.error()))
    return
  }
  if (!url.pathname.startsWith('/assets/') && !url.pathname.startsWith('/src/') && !url.pathname.startsWith('/node_modules/') && !url.pathname.startsWith('/@vite/')) return
  event.respondWith(fetch(request).catch(async () => (await caches.open(SHELL)).match(request) || Response.error()))
})
