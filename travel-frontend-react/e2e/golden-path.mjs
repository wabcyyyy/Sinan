// 金路径 E2E：注册/登录 → ChatIntake 一句话创建 → 生成进度 → 首页预览板（拍板①
// 2026-10-02：done 留在首页看预览，已无自动跳详情页）→ 点 CTA「打开完整行程」
// 进行程详情页看地图。
// 纯 playwright-core 脚本（无 @playwright/test，不引新依赖），浏览器用系统
// Edge/Chrome（channel 探测），目标是活栈：前端 5173 + FastAPI 8000（start-all.ps1
// 或 docker dev）。生成走真实 LLM（同 tests/api 口径），分钟级耗时属正常。
//
//   cd travel-frontend-react && npm run e2e:golden
//
// 环境变量：E2E_BASE_URL（默认 http://localhost:5173）、E2E_API_URL（默认
// http://127.0.0.1:8000，仅探活）、E2E_CHANNEL（msedge|chrome|bundled，默认依次
// 尝试 msedge→chrome）、E2E_HEADED=1（有头调试）、E2E_GEN_TIMEOUT_MS（默认
// 1200000——真实 LLM 全趟生成实测 8~15 分钟（2026-10-03 金路径 DB 实据：行程
// 76/77 各 779s/820s 到终态），须活得比它久；只有失败场景才等满，成功路径
// 预览板一出现即返回）。
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import pw from 'playwright-core'
import { assertGoldenDelivery } from './delivery-gate.mjs'

const BASE = process.env.E2E_BASE_URL || 'http://localhost:5173'
const API = process.env.E2E_API_URL || 'http://127.0.0.1:8000'
const GEN_TIMEOUT = Number(process.env.E2E_GEN_TIMEOUT_MS || 1_200_000)
const ART = fileURLToPath(new URL('./artifacts/', import.meta.url))
const PASSWORD = 'golden-path-pass-123'

const t0 = Date.now()
const elapsed = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`
const step = (name) => console.log(`[e2e ${elapsed()}] ${name}`)

async function assertStackUp() {
  for (const [name, url] of [['前端 ' + BASE, BASE + '/'], ['后端 ' + API, API + '/api/test/hello']]) {
    try {
      const res = await fetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (err) {
      throw new Error(`${name} 不可达（${err.message}）。先跑 start-all.ps1（或 docker dev）再执行 E2E。`)
    }
  }
}

async function launchBrowser() {
  const headless = process.env.E2E_HEADED !== '1'
  const channels = process.env.E2E_CHANNEL ? [process.env.E2E_CHANNEL] : ['msedge', 'chrome']
  let lastErr = null
  for (const channel of channels) {
    try {
      return await pw.chromium.launch({ ...(channel === 'bundled' ? {} : { channel }), headless })
    } catch (err) {
      lastErr = err
    }
  }
  throw new Error(
    `找不到可用的浏览器（试过 ${channels.join(' → ')}）。Windows/CI 自带 Edge/Chrome 可直接跑，` +
      `或设 E2E_CHANNEL 指定；bundled 需先安装 playwright 浏览器。原因：${lastErr?.message ?? '未知'}`,
  )
}

let browser = null
let page = null
try {
  await assertStackUp()
  step(`活栈就绪：${BASE} + ${API}`)

  browser = await launchBrowser()
  const pageErrors = []
  const context = await browser.newContext({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 1280, height: 800 } })
  page = await context.newPage()
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  // ① 注册即登录（新用户金路径）：UI 注册 → 自动登录 → 落到 /trips
  const username = `e2e_golden_${Date.now().toString(36)}`
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('金路径')
  await page.getByLabel('账号').fill(username)
  await page.getByLabel('密码').fill(PASSWORD)
  await page.getByRole('button', { name: '注册并登录' }).click()
  await page.waitForURL('**/trips', { timeout: 30_000 })
  step(`① 注册并登录：${username}`)

  // ② ChatIntake 一句话创建：city/days/persons 三件套一句话给齐，走 clarify；
  //    LLM 一轮没抽齐就点 chips 兜底（最多 4 轮，同漏斗真实形态）。
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
  const composer = page.locator('input[aria-label="说说你的旅行想法"]')
  await composer.waitFor({ state: 'visible', timeout: 30_000 })
  await composer.fill('想去成都玩 2 天，两个人，预算 3000，想吃火锅')
  await page.getByRole('button', { name: '发送' }).click()

  const confirmBtn = page.locator('.intake-start')
  let ready = false
  for (let round = 1; round <= 4 && !ready; round += 1) {
    try {
      await confirmBtn.waitFor({ state: 'visible', timeout: 45_000 })
      ready = true
    } catch {
      const chips = page.locator('.intake-chips button')
      if (await chips.count()) {
        step(`② clarify 第 ${round} 轮未就绪，点选首个候选继续`)
        await chips.first().click()
      } else {
        await composer.fill('两个人一起出行')
        await page.getByRole('button', { name: '发送' }).click()
      }
    }
  }
  if (!ready) throw new Error('clarify 多轮后仍未出现「就这样，开始规划」确认区')
  await confirmBtn.click()
  step('② 一句话槽位集齐，已开始规划')

  // ③ 生成进度 → 首页预览板：预览面板出四段进度条；完成判定不再等跳转（拍板①
  //    移除了 900ms 自动跳详情页），改为等 .trip-board 预览板上屏（等满 GEN_TIMEOUT），
  //    再点 .trip-board-cta「打开完整行程」走 SPA 路由进详情页。
  await page.locator('ol.planning-stages').waitFor({ state: 'visible', timeout: 20_000 })
  step('③ 生成进度已可见（SSE + 轮询并行监控）')
  await page.locator('.trip-board').waitFor({ state: 'visible', timeout: GEN_TIMEOUT })
  step('③ 首页预览板已出现')
  await page.locator('.trip-board-cta').click()
  await page.waitForURL(/\/trips\/\d+/, { timeout: 30_000 })
  const tripId = page.url().match(/\/trips\/(\d+)/)?.[1]
  step(`③ 已从预览板进入行程 ${tripId}`)
  const deliveryResponse = await context.request.get(`${BASE}/api/itinerary/${tripId}`, { timeout: 30_000 })
  if (!deliveryResponse.ok()) throw new Error(`交付核验 HTTP ${deliveryResponse.status()}`)
  const delivery = await deliveryResponse.json()
  if (delivery.code !== 200) throw new Error(`交付核验失败：${delivery.message}`)
  assertGoldenDelivery(delivery.data)
  step('③ 两天完整交付、住宿晚数与最终预算均已核验')

  // ④ 详情页地图：懒加载 chunk + maplibre 初始化；无坐标点位时是空态（金路径不该走到）
  await page.locator('.trip-map').waitFor({ state: 'visible', timeout: 60_000 })
  if (await page.locator('.trip-map.is-empty').count()) {
    throw new Error(`行程 ${tripId} 没有任何带坐标的点位（地图空态）——生成质量回归`)
  }
  await page.locator('.maplibregl-canvas').waitFor({ state: 'visible', timeout: 60_000 })
  await page.locator('.map-pin').first().waitFor({ state: 'visible', timeout: 60_000 })
  const pinCount = await page.locator('.map-pin').count()
  const title = (await page.locator('h1').first().textContent())?.trim() || ''
  if (!title) throw new Error('详情页标题为空')
  step(`④ 地图已出现：${pinCount} 个 pin，标题「${title}」`)

  if (pageErrors.length) throw new Error(`页面出现未捕获异常：\n${pageErrors.join('\n')}`)

  console.log(`\n[e2e] 金路径通过 ✔ 用户=${username} 行程=${tripId} 总耗时 ${elapsed()}`)
  await browser.close()
  process.exit(0)
} catch (err) {
  console.error(`\n[e2e] 金路径失败 ✘（${elapsed()}）`)
  console.error(err instanceof Error ? err.message : err)
  // 收尾加固：失败截图/close 曾在失败路径挂死，进程迟迟不退、被外部按墙钟强杀——
  // 整段收尾（截图 + close）用 Promise.race 兜底 15s，超时也必走 process.exit(1)。
  const deadline = new Promise((resolve) => setTimeout(resolve, 15_000))
  const cleanup = (async () => {
    try {
      mkdirSync(ART, { recursive: true })
      if (page) await page.screenshot({ path: `${ART}golden-path-failure.png`, fullPage: true })
    } catch { /* 截图失败不影响失败结论 */ }
    await browser?.close().catch(() => {})
  })()
  await Promise.race([cleanup, deadline])
  process.exit(1)
}
