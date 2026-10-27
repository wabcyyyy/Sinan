// 正式页面 + 本地隔离后端，复用四份真实视觉输出；每个 case 最多 80 秒。
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const scenario = process.argv[2] || 'guide'
assert(['guide', 'booking', 'guide_kyoto', 'transport'].includes(scenario))
const base = 'http://127.0.0.1:15173'
const out = resolve(process.env.P3_RUN_DIR || '../.validation/p3-browser-20261024-2028', `${scenario}-${Date.now()}`)
const fixtures = resolve('../travel-agent-python/tests/fixtures/screenshots')
mkdirSync(out, { recursive: true })
const result = { scenario, startedAt: new Date().toISOString(), steps: [], responses: [], pageErrors: [], mode: 'real browser/UI/API/auth/SQLite/solver; recorded vision and geographic observations' }
const started = Date.now()
let browser, context, page
const pending = []
const save = () => writeFileSync(resolve(out, `${scenario}-browser.json`), JSON.stringify(result, null, 2))
const step = name => { result.steps.push({ name, ms: Date.now() - started }); console.log(name); save() }
const budget = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser?.close(); process.exit(2) }, 80_000)

async function prepareCreation(japanese) {
  await page.goto(`${base}/`)
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill(japanese ? '京都 一天 一人 2026-10-25' : 'Paris 一天 一人 2026-10-25')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor({ state: 'visible' })
  const options = page.locator('details').filter({ has: page.locator('summary', { hasText: '按时间要求安排' }) })
  await options.locator('summary').click()
  await options.getByLabel('国家代码', { exact: true }).fill(japanese ? 'JP' : 'FR')
  await options.getByLabel('当地时区', { exact: true }).fill(japanese ? 'Asia/Tokyo' : 'Europe/Paris')
  await options.getByLabel('计价币种', { exact: true }).fill(japanese ? 'JPY' : 'EUR')
  await options.getByRole('button', { name: '确认并启用时间约束规划', exact: true }).click()
  return page.locator('.intake-confirm .screenshot-import')
}

async function screenshotInput(section, name, { bookingOnly = false } = {}) {
  await section.locator('summary').click()
  if (!bookingOnly) await section.getByLabel('截图类型').selectOption(name.startsWith('guide') ? 'guide' : 'booking')
  await section.locator('input[type=file]').setInputFiles(resolve(fixtures, `${name}.png`))
  await section.getByRole('button', { name: '识别截图字段', exact: true }).click()
  await section.locator('.screenshot-fields').waitFor({ state: 'visible' })
  await section.getByRole('button', { name: /^name：/ }).click()
  await section.locator('[aria-label="当前字段证据区域"]').waitFor({ state: 'visible' })
  await section.getByRole('button', { name: '查询地点实体', exact: true }).click()
  const radio = section.locator('input[type=radio]')
  await radio.first().waitFor({ state: 'visible' })
  if (name === 'transport') assert.equal(await radio.count(), 4, '保留真实观测中的四个车站实体供选择')
  assert.equal(await radio.first().isChecked(), false, '不得自动选择第一实体')
  await radio.first().check()
  return section
}

async function create(name) {
  const section = await prepareCreation(name === 'guide_kyoto')
  await screenshotInput(section, name)
  if (name === 'booking') {
    assert.equal(await section.getByLabel('结束当地日期').inputValue(), '')
    assert.match(await section.textContent(), /未识别结束当地日期/)
    await section.locator('input[type=checkbox]').check()
    await section.getByRole('button', { name: '确认加入需求表单', exact: true }).click()
    await section.getByRole('status').filter({ hasText: '日期' }).waitFor({ state: 'visible' })
    assert.equal(result.responses.filter(r => r.path.endsWith('/image-intent/confirm')).at(-1)?.status, 422)
    await section.getByLabel('结束当地日期').fill('2026-10-25')
  } else {
    assert.equal(await section.getByLabel('采用方式').inputValue(), 'candidate')
    await section.getByLabel('采用方式').selectOption('required')
  }
  await section.getByRole('button', { name: '确认加入需求表单', exact: true }).click()
  await section.getByRole('status').filter({ hasText: '已生成待采用输入' }).waitFor({ state: 'visible' })
  step(`${name}: uploaded, region preview, explicit entity and requirement confirmation`)
  await page.screenshot({ path: resolve(out, `${scenario}-requirements.png`), fullPage: true })
  await page.locator('.intake-start').click()
  await page.locator('.trip-board-cta').waitFor({ state: 'visible', timeout: 15_000 })
  await page.locator('.trip-board-cta').click()
  await page.waitForURL(/\/trips\/\d+/)
  const id = Number(page.url().match(/\/trips\/(\d+)/)[1])
  const response = await context.request.get(`${base}/api/itinerary/${id}`)
  const detail = (await response.json()).data
  assert.equal(detail.genState, 'COMPLETED')
  assert.equal(detail.dayList[0].items.length, 1)
  const item = detail.dayList[0].items[0]
  assert.equal(detail.dayList[0].constraintPlanning.facts[0].sourceEntityId, name === 'guide_kyoto' ? 'osm:way:336641107' : 'osm:relation:7515426')
  assert.equal(detail.dayList[0].constraintPlanning.status, 'UNKNOWN')
  if (name === 'booking') {
    assert.equal(item.startTime.slice(0, 5), '14:00'); assert.equal(item.endTime.slice(0, 5), '16:00')
    assert.equal(item.costCurrency, 'EUR')
  }
  await page.reload()
  await page.getByRole('button', { name: '旅行资料', exact: true }).waitFor({ state: 'visible' })
  const refreshed = (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
  assert.deepEqual(refreshed.dayList, detail.dayList)
  result.tripId = id; result.detail = refreshed
  step(`${name}: real plan_days/solver persisted and refresh stable`)
  return { id, detail }
}

try {
  browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
  context = await browser.newContext({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 1360, height: 900 } })
  await context.route('**/*', route => {
    const host = new URL(route.request().url()).hostname
    return ['127.0.0.1', 'localhost', ''].includes(host) ? route.continue() : route.abort()
  })
  page = await context.newPage(); page.setDefaultTimeout(8_000); page.setDefaultNavigationTimeout(8_000)
  page.on('pageerror', error => result.pageErrors.push(String(error)))
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/image-intent\/(document|resolve|confirm)$|\/itinerary\/generate$|\/preparation$/.test(path)) {
      pending.push(response.json().then(body => { result.responses.push({ path, status: response.status(), body }) }).catch(() => {}))
    }
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('P3 匿名验收')
  await page.getByLabel('账号', { exact: true }).fill(`p3_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('p3-browser-pass-123')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips')
  step('registered and authenticated using real session cookie')
  const { id, detail } = await create(scenario === 'transport' ? 'guide_kyoto' : scenario)
  if (scenario === 'transport') {
    await page.getByRole('button', { name: '旅行资料', exact: true }).click()
    await page.getByRole('button', { name: '已订资料', exact: true }).click()
    const section = page.locator('.preparation-panel .screenshot-import')
    await screenshotInput(section, 'transport', { bookingOnly: true })
    await section.locator('input[type=checkbox]').check()
    await section.getByRole('button', { name: '确认并保存已订资料', exact: true }).click()
    await section.getByRole('status').filter({ hasText: '已保存到私有已订资料' }).waitFor({ state: 'visible' })
    await page.reload()
    await page.getByRole('button', { name: '旅行资料', exact: true }).click()
    await page.getByRole('button', { name: '已订资料', exact: true }).click()
    const prep = (await (await context.request.get(`${base}/api/itinerary/${id}/preparation`)).json()).data
    const booking = prep.data.bookings[0]
    assert.equal(booking.startAt, '2026-10-25T23:00:00+09:00')
    assert.equal(booking.endAt, '2026-10-26T06:00:00+09:00')
    assert.equal(booking.originalCurrency, 'JPY'); assert.equal(booking.originalAmount, '1200')
    assert.deepEqual((await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data.dayList, detail.dayList)
    result.preparation = prep
    step('overnight booking saved via preparation CAS, refresh stable, formal plan unchanged')
  }
  await page.screenshot({ path: resolve(out, `${scenario}-saved.png`), fullPage: true })
  if (scenario === 'transport') {
    const loggedOut = page.waitForResponse(response => response.url().endsWith('/api/auth/logout'))
    await page.getByRole('button', { name: '登出', exact: true }).click()
    await loggedOut
    assert.equal((await context.request.get(`${base}/api/itinerary/${id}/preparation`)).status(), 401)
    await page.goto(`${base}/login`)
    await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
    await page.getByLabel('怎么称呼你').fill('P3 隔离检查')
    await page.getByLabel('账号', { exact: true }).fill(`p3_${Date.now().toString(36)}`)
    await page.getByLabel('密码', { exact: true }).fill('p3-browser-pass-123')
    await page.getByRole('button', { name: '注册并登录', exact: true }).click()
    await page.waitForURL('**/trips')
    const forbidden = await context.request.get(`${base}/api/itinerary/${id}/preparation`)
    assert([403, 404].includes(forbidden.status()))
    result.isolation = { loggedOutStatus: 401, otherAccountStatus: forbidden.status() }
    step('logout clears access and another real account cannot read the booking')
  }
  assert.deepEqual(result.pageErrors, [])
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error.stack || error)
  if (page) { result.visibleText = (await page.locator('body').innerText().catch(() => '')).slice(-12_000); await page.screenshot({ path: resolve(out, `${scenario}-failure.png`), fullPage: true }).catch(() => {}) }
  console.error(result.error); process.exitCode = 1
} finally {
  clearTimeout(budget); await Promise.allSettled(pending)
  result.seconds = (Date.now() - started) / 1000; save(); await browser?.close()
  console.log(JSON.stringify({ scenario, status: result.status, seconds: result.seconds, output: resolve(out, `${scenario}-browser.json`) }))
}
