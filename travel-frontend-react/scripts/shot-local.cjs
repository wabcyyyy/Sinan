/**
 * 本机无头截图（v2.6 W5 转正）：CDP 无头截图，「任意视口 × 亮/暗 × 任意路由」参数化，
 * 用于桌面/移动断点与双主题的视觉验证。
 *
 * 用法：MSYS_NO_PATHCONV=1 node scripts/shot-local.cjs 1440 900 / /trips
 *   - 前置：npm run dev（localhost:5173，/api 代理到 8000）；
 *   - 会预置 localStorage（token/username/role 与 ta-appearance-v1）：假 token 被 401 清会话后
 *     落到登录页，但 Shell/导航照常渲染（可用来验证顶栏、toast 管道、主题）；
 *   - 已登录的详情页数据流验证仍需真实登录态（此脚本不做凭据注入）。
 *   - 输出到 .tmp-shots/（脚本自建；注意 Git Bash 下裸 `/` 参数会被 MSYS 转义 → MSYS_NO_PATHCONV=1）。
 */
const { spawn } = require('child_process')
const fs = require('fs')
const path = require('path')
const http = require('http')

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const APP = 'http://localhost:5173'
const PORT = 9225
const OUTDIR = path.join(__dirname, '..', '.tmp-shots')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port: PORT, path: pathname }, (res) => {
      let d = ''
      res.on('data', (c) => (d += c))
      res.on('end', () => {
        try {
          resolve(JSON.parse(d))
        } catch (e) {
          reject(e)
        }
      })
    })
    req.on('error', reject)
  })
}

async function main() {
  const [w = '1440', h = '900', ...routes] = process.argv.slice(2)
  if (!routes.length) throw new Error('pass routes')
  fs.mkdirSync(OUTDIR, { recursive: true })

  const chrome = spawn(
    CHROME,
    ['--headless=new', '--disable-gpu', '--no-first-run', `--remote-debugging-port=${PORT}`, `--window-size=${w},${h}`, 'about:blank'],
    { stdio: 'ignore' }
  )

  let page = null
  for (let i = 0; i < 50; i++) {
    try {
      const list = await getJson('/json/list')
      page = list.find((t) => t.type === 'page')
      if (page) break
    } catch {
      /* retry */
    }
    await sleep(200)
  }
  if (!page) {
    chrome.kill()
    throw new Error('no page')
  }

  const ws = new WebSocket(page.webSocketDebuggerUrl)
  let id = 0
  const pending = new Map()
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const msgId = ++id
      pending.set(msgId, { resolve, reject })
      ws.send(JSON.stringify({ id: msgId, method, params }))
    })

  await new Promise((resolve, reject) => {
    ws.onopen = resolve
    ws.onerror = reject
  })
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.error) reject(new Error(msg.error.message))
      else resolve(msg.result)
    }
  }

  await send('Page.enable')
  await send('Runtime.enable')
  await send('Page.navigate', { url: APP + '/' })
  await sleep(700)
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('token','dev-preview-token');localStorage.setItem('username','预览用户');localStorage.setItem('role','user');`,
  })

  for (const route of routes) {
    for (const dark of [false, true]) {
      await send('Runtime.evaluate', {
        expression: `localStorage.setItem('ta-appearance-v1', '${JSON.stringify({ scheme: 'default', dark, density: 'comfortable', reduceMotion: true })}')`,
      })
      await send('Page.navigate', { url: APP + route })
      await sleep(1800)
      const tag = `${w}x${h}-${route === '/' ? 'home' : route.replace(/\//g, '-').replace(/^-/, '')}-${dark ? 'dark' : 'light'}`
      const shot = await send('Page.captureScreenshot', { format: 'png' })
      const file = path.join(OUTDIR, `${tag}.png`)
      fs.writeFileSync(file, Buffer.from(shot.data, 'base64'))
      console.log('saved', `${tag}.png`, fs.statSync(file).size)
    }
  }

  ws.close()
  chrome.kill()
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
