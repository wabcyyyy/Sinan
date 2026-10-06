// 一次性自注销 SW（R2-F8 拍板 2026-10-05）：清退 Vue 时代注册的旧 Service Worker 壳。
// 旧 SW 的 update check 拉到本文件即换装 → activate 时清全部缓存并注销自身；
// 当前页面在缓存清空后由网络直出，下一次导航即是干净的新壳。
// 全量用户清退完成后本文件可删——仓库里没有任何 navigator.serviceWorker.register
// 调用，保留它只是给历史上沾染过旧 SW 的浏览器"喂最后一剂药"。
self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys()
      await Promise.all(names.map((name) => caches.delete(name)))
      await self.registration.unregister()
      const clients = await self.clients.matchAll({ type: 'window' })
      for (const client of clients) {
        client.navigate(client.url).catch(() => {})
      }
    })(),
  )
})
