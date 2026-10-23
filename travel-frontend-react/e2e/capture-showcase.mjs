// Capture actual demo UI at 2x resolution; no compositing or enlarged source images.
// Run against `npm run dev -- --mode demo`, or a demo preview using E2E_BASE_URL.
import pw from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const base = process.env.E2E_BASE_URL || 'http://127.0.0.1:5173'
const output = fileURLToPath(new URL('../../docs/public/preview/', import.meta.url))
mkdirSync(output, { recursive: true })
const browser = await pw.chromium.launch({ channel: process.env.E2E_CHANNEL || 'msedge' })
try {
  const context = await browser.newContext({ viewport: { width: 1200, height: 860 }, deviceScaleFactor: 2, reducedMotion: 'reduce' })
  const page = await context.newPage()
  const ready = async () => {
    await page.evaluate(() => document.fonts.ready)
    await page.locator('img').evaluateAll(images => Promise.all(images.filter(img => img.loading !== 'lazy' || img.getBoundingClientRect().top < innerHeight).map(img => img.decode().catch(() => {}))))
  }
  const capture = async name => { await ready(); await page.screenshot({ path: `${output}${name}.png` }) }
  await page.goto(base)
  await page.getByRole('button', { name: '知道了，开始体验', exact: true }).click()
  await page.getByRole('heading', { name: /去一座城/ }).waitFor()
  await capture('showcase-home-20261023')
  await page.goto(`${base}/itineraries/-1`)
  await page.locator('.public-day-nav').waitFor()
  await capture('showcase-route-20261023')
  await page.locator('.public-guide').scrollIntoViewIfNeeded()
  await ready()
  await page.locator('.public-guide').screenshot({ path: `${output}showcase-guide-20261023.png` })
  await page.goto(`${base}/trips/101`)
  await page.getByRole('button', { name: /^DAY 02/ }).click()
  await page.getByRole('button', { name: '对话调整', exact: true }).click()
  await page.getByRole('button', { name: '把第 2 天安排更松弛一些', exact: true }).click()
  await page.getByLabel('局部修改预览', { exact: true }).waitFor()
  await page.locator('.detail-workspace.workbench').evaluate(el => window.scrollTo({ top: el.getBoundingClientRect().top + scrollY - 96, behavior: 'instant' }))
  await capture('showcase-edit-20261023')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${base}/itineraries/-1`)
  await page.locator('.public-day-nav').waitFor()
  await capture('showcase-route-mobile-20261023')
  console.log(`Saved 2x PNG screenshots to ${output}`)
} finally { await browser.close() }
