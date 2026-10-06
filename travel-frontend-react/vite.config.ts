import { defineConfig, type Plugin } from 'vite'

// R2-F8（2026-10-05 拍板）：dev 源（:5173）历史上从未注册过 SW，对 /sw.js 回 404
// 防再沾染；生产源由 public/sw.js（自注销 SW）负责清退旧壳，任何静态主机通用。
function noServiceWorkerInDev(): Plugin {
  return {
    name: 'no-sw-in-dev',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url?.split('?')[0] === '/sw.js') {
          res.statusCode = 404
          res.end()
          return
        }
        next()
      })
    },
  }
}

export default defineConfig({
  plugins: [noServiceWorkerInDev()],
  esbuild: { jsx: 'automatic', jsxImportSource: 'react' },
  build: {
    rollupOptions: {
      output: {
        // react vendor 单独成块：业务代码改动不再让框架层缓存失效
        manualChunks: { react: ['react', 'react-dom'] },
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } },
  },
})
