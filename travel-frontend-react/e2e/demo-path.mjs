// Offline browser delivery: no backend or model secrets. Also runs against nginx in compose CI.
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import pw from 'playwright-core'

const base = process.env.E2E_BASE_URL || 'http://localhost:5173'
const channel = process.env.E2E_CHANNEL || 'msedge'
const artifacts = fileURLToPath(new URL('./artifacts/', import.meta.url))
const browser = await pw.chromium.launch(channel === 'bundled' ? {} : { channel })
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' })
  const page = await context.newPage()
  const apiRequests = []
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/')) { apiRequests.push(url.pathname); return route.abort() }
    if (url.origin !== new URL(base).origin) return route.abort()
    return route.continue()
  })
  await page.addInitScript(() => {
    localStorage.setItem('sinan-username', 'existing-user-do-not-touch')
    const original = HTMLCanvasElement.prototype.getContext
    HTMLCanvasElement.prototype.getContext = function (kind, ...args) {
      if (String(kind).startsWith('webgl')) return null
      return original.call(this, kind, ...args)
    }
  })
  await page.goto(`${base}/demo`, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '在成都，把脚步放慢' }).waitFor()
  await page.getByRole('button', { name: '播放', exact: true }).click()
  await page.getByRole('button', { name: '暂停', exact: true }).click()
  const paused = await page.locator('output').textContent()
  await page.waitForTimeout(1400)
  assert.equal(await page.locator('output').textContent(), paused)
  await page.getByRole('button', { name: '重置', exact: true }).click()
  for (let i = 0; i < 6; i++) await page.getByRole('button', { name: '单步', exact: true }).click()
  assert.match(await page.locator('output').textContent(), /行程就绪/)
  assert.equal(await page.locator('.board-day-chip').count(), 2)
  await page.getByRole('button', { name: 'DAY 02', exact: false }).click()
  await page.getByRole('heading', { name: '杜甫草堂', exact: true }).waitFor()
  await page.getByRole('button', { name: '查看编辑示例', exact: true }).click()
  await page.locator('.chat-diff-box').waitFor()
  assert.equal(await page.locator('.board-budget-total').textContent(), '￥570')
  await page.getByRole('button', { name: '应用到行程', exact: true }).click()
  assert.equal(await page.getByRole('heading', { name: '杜甫草堂', exact: true }).count(), 0)
  assert.equal(await page.locator('.board-budget-total').textContent(), '￥520')
  assert.ok(await page.locator('.demo-links a').count() >= 4)
  assert.match(await page.locator('aside').textContent(), /WebGL|地图未能启动/)
  mkdirSync(artifacts, { recursive: true })
  await page.screenshot({ path: `${artifacts}/demo-desktop.png`, fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
  await page.screenshot({ path: `${artifacts}/demo-mobile.png`, fullPage: true })
  await page.reload({ waitUntil: 'networkidle' })
  assert.match(await page.locator('output').textContent(), /1\/7/)
  await page.getByRole('button', { name: '播放', exact: true }).click()
  await page.getByRole('button', { name: '查看编辑示例', exact: true }).waitFor({ state: 'visible' })
  await page.waitForFunction(() => document.querySelector('output')?.textContent?.includes('行程就绪'), null, { timeout: 15000 })
  await page.getByRole('button', { name: '重置', exact: true }).click()
  assert.match(await page.locator('output').textContent(), /1\/7/)
  assert.equal(await page.evaluate(() => localStorage.getItem('sinan-username')), 'existing-user-do-not-touch')
  assert.deepEqual(apiRequests, [])
  assert.deepEqual(errors, [])
  // A second context exercises real MapLibre initialization with a local empty style.
  // It proves rendering and marker placement, not external tile availability.
  const mapContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' })
  const mapPage = await mapContext.newPage()
  await mapPage.route('**/*', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.startsWith('/api/')) { apiRequests.push(url.pathname); return route.abort() }
    if (url.href === 'https://tiles.openfreemap.org/styles/liberty') return route.fulfill({
      contentType: 'application/json', body: JSON.stringify({ version: 8, sources: {}, layers: [] }),
    })
    if (url.origin !== new URL(base).origin) return route.abort()
    return route.continue()
  })
  await mapPage.goto(`${base}/demo`)
  for (let i = 0; i < 6; i++) await mapPage.getByRole('button', { name: '单步', exact: true }).click()
  await mapPage.locator('.map-pin-wrap').first().waitFor({ state: 'visible' })
  assert.equal(await mapPage.locator('.map-pin-wrap').count(), 6)
  await mapPage.screenshot({ path: `${artifacts}/demo-map.png`, fullPage: true })
  assert.deepEqual(apiRequests, [])
  await mapContext.close()
  console.log('PASS: replay, pause, step, reset, scoped edit, budget, refresh, mobile and map fallback; zero API calls')
} finally {
  await browser.close()
}
