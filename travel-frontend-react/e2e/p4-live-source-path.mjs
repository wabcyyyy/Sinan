// 同轮真实输出回放进入正式 UI/API/数据库/worker；浏览器阶段无外网/模型。
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const out = resolve(process.env.P4_RUN_DIR)
mkdirSync(out, { recursive: true })
const result = { steps: [], pageErrors: [], method: 'real UI/API/SQLite/worker; same-run real model and source snapshots replayed' }
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
const timer = setTimeout(async () => { result.status = 'timeout'; writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result)); await browser.close(); process.exit(2) }, 14000)
try {
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1360, height: 1000 } })
  await context.route('**/*', r => ['127.0.0.1', 'localhost', ''].includes(new URL(r.request().url()).hostname) ? r.continue() : r.abort())
  const page = await context.newPage(); page.setDefaultTimeout(4500)
  page.on('pageerror', e => result.pageErrors.push(String(e)))
  await page.goto('http://127.0.0.1:15174/login')
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('P4 真实来源验收')
  await page.getByLabel('账号', { exact: true }).fill(`p4live_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('p4-local-live-123')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips')
  await page.goto('http://127.0.0.1:15174/')
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill('11月3日杭州一日，一人不住宿，100元硬上限；中国丝绸博物馆09:00至13:00固定停留4小时。')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor({ state: 'visible' })
  const settings = page.locator('details').filter({ has: page.locator('summary', { hasText: '按时间要求安排' }) })
  await settings.locator('summary').click()
  await settings.getByLabel('优先核实影响安排的资料', { exact: true }).check()
  await settings.getByLabel('允许联网来源搜索（可能消耗 AI 额度）', { exact: true }).check()
  await settings.getByRole('button', { name: '更新目的地口径', exact: true }).click()
  await page.locator('.intake-start').click()
  await page.locator('.trip-board-cta').waitFor({ state: 'visible', timeout: 8000 })
  await page.locator('.trip-board-cta').click()
  await page.waitForURL(/\/trips\/\d+/)
  const id = Number(page.url().match(/\/trips\/(\d+)/)[1])
  const detail = async () => (await (await context.request.get(`http://127.0.0.1:15174/api/itinerary/${id}`)).json()).data
  const before = await detail()
  result.trip = before
  assert.equal(before.genState, 'COMPLETED')
  assert.equal(before.dayList[0].items[0].poiName, '中国丝绸博物馆')
  assert.equal(before.dayList[0].constraintPlanning.status, 'OPTIMAL')
  assert(before.dayList[0].constraintPlanning.activeEvidence.queries.some(q => q.outcome === 'updated'))
  await page.screenshot({ path: resolve(out, 'created-real-source.png'), fullPage: true })
  result.steps.push('real registration/intake/opt-in/worker/source adoption/persisted plan')
  await page.reload()
  await page.getByRole('button', { name: '对话调整', exact: true }).waitFor({ state: 'visible' })
  assert.deepEqual((await detail()).dayList, before.dayList)
  assert.equal(result.pageErrors.length, 0)
  result.steps.push('refresh preserves exact persisted day and source report')
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error); process.exitCode = 1
} finally {
  clearTimeout(timer); await browser.close()
  writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify({ status: result.status, steps: result.steps, error: result.error }))
}
