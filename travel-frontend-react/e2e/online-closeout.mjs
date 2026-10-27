// 当次在线链路；不回放模型、地点或路线。候选/完整行程/保存时点另记，不沿用旧51.4%。
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const out = resolve(process.env.SINAN_CLOSEOUT_BROWSER_DIR)
mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:15176'
const result = { scope: 'live clarify/research/public places/routes -> real worker/CP/SQLite -> appointment/Diff/apply/refresh', responses: [], events: [], pageErrors: [], timing: {} }
const save = () => writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result, null, 2))
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1080 }, recordVideo: { dir: resolve(out, 'video') } })
let page
const pending = []
const timer = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser.close(); process.exit(2) }, 85000)
try {
  // 浏览器仅访问本地；真实外部资料由带预算闸的后端取得。
  await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  await context.addInitScript(() => {
    window.__closeoutTimes = {}
    const observer = new MutationObserver(() => {
      if (document.querySelector('[aria-label="尚未排程的研究候选"]') && !window.__closeoutTimes.firstCandidateAt) window.__closeoutTimes.firstCandidateAt = performance.timeOrigin + performance.now()
      if (document.querySelector('.trip-board-cta') && !window.__closeoutTimes.fullTripAt) window.__closeoutTimes.fullTripAt = performance.timeOrigin + performance.now()
    })
    observer.observe(document, { childList: true, subtree: true })
  })
  page = await context.newPage(); page.setDefaultTimeout(7000)
  page.on('pageerror', error => result.pageErrors.push(String(error)))
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/replans|apply-plans|\/generate$|\/city\/clarify$/.test(path)) pending.push(response.json().then(body => {
      result.responses.push({ path, http: response.status(), body }); save()
    }).catch(() => {}))
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('匿名在线验收')
  await page.getByLabel('账号', { exact: true }).fill(`live_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('anonymous-local-20261025')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips'); await page.goto(`${base}/`)
  const message = '2026年11月3日到Paris，一人一天不住宿，到达日14:00以后开始18:00前结束，只安排Musée du Louvre（卢浮宫）和Musée d’Orsay（奥赛博物馆），两馆都必去，每天最多2个景点，排除埃菲尔铁塔。预算100欧元硬上限。喜欢艺术，先去哪家都可以，不排餐饮。每馆计划90分钟。' + (process.env.SINAN_CLOSEOUT_REGRESSION ? ' 这是同名撇号排版问题修复后的回归案例，旅行要求不变。' : '')
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill(message)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor({ state: 'visible', timeout: 16000 })
  const planner = page.locator('details').filter({ has: page.locator('summary', { hasText: '按时间要求安排' }) }).first()
  await planner.locator('summary').click()
  await planner.getByLabel('国家代码').fill('FR')
  await planner.getByLabel('当地时区').fill('Europe/Paris')
  await planner.getByLabel('计价币种').fill('EUR')
  await planner.getByRole('button', { name: /确认并启用时间约束规划|更新目的地口径/ }).click()
  await page.screenshot({ path: resolve(out, '01-confirmed-input.png'), fullPage: true })
  result.timing.submitAt = Date.now(); save()
  await page.locator('.intake-start').click()
  await page.locator('.trip-board-cta').waitFor({ state: 'visible', timeout: 45000 })
  result.timing = { ...result.timing, ...await page.evaluate(() => window.__closeoutTimes) }
  await page.locator('.trip-board-cta').click(); await page.waitForURL(/\/trips\/\d+/)
  const id = Number(page.url().match(/\/trips\/(\d+)/)[1]); result.itineraryId = id
  const detail = async () => (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
  const before = await detail(); result.initial = before; result.timing.initialSavedObservedAt = Date.now(); save()
  assert.equal(before.genState, 'COMPLETED')
  assert.equal(before.dayList.flatMap(day => day.items).length, 2)
  assert.equal(before.budgetCurrency, 'EUR')
  assert.equal(before.requirementsStruct.budgetPolicy.mode, 'hard_cap')
  assert(before.requirementsStruct.requiredPlaces.length >= 2)
  assert(before.requirementsStruct.excludedPlaces.length >= 1)
  assert.equal(before.requirementsStruct.constraintPlanning.activeEvidence, null)
  assert(before.dayList[0].constraintPlanning.routes.every(route => route.source === 'osrm'))
  assert.equal(before.dayList[0].constraintPlanning.status, 'UNKNOWN')
  await page.screenshot({ path: resolve(out, '02-created-with-disclosure.png'), fullPage: true })
  await page.getByRole('button', { name: '对话调整', exact: true }).click()
  const composer = page.getByRole('region', { name: '约束行程修改' })
  const target = before.dayList.flatMap(day => day.items).find(item => /orsay|奥赛/i.test(item.poiName))
  assert(target)
  await composer.getByLabel('要调整的安排').selectOption(String(target.id))
  await composer.locator('summary', { hasText: '或手动填写变化与保留范围' }).click()
  await composer.getByLabel('变化类型').selectOption('appointment')
  await composer.getByLabel('目标日期').selectOption('1')
  await composer.getByLabel('开始时间', { exact: true }).fill('16:00')
  await composer.getByLabel('结束／截止时间', { exact: true }).fill('17:30')
  await composer.getByRole('button', { name: '比较修改方案', exact: true }).click()
  await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().click()
  await page.getByRole('button', { name: '应用到行程', exact: true }).waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await page.screenshot({ path: resolve(out, '03-diff-before-confirmation.png'), fullPage: true })
  await Promise.all([
    page.waitForResponse(response => /apply-plans$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST'),
    page.getByRole('button', { name: '应用到行程', exact: true }).click(),
  ])
  const after = await detail(); result.final = after; result.timing.finalSavedObservedAt = Date.now(); save()
  assert(after.requirementsStruct.fixedArrangements.some(fixed => /orsay|奥赛/i.test(fixed.name) && fixed.startTime === '16:00' && fixed.endTime === '17:30'))
  for (const key of ['requiredPlaces', 'excludedPlaces', 'budgetPolicy']) assert.deepEqual(after.requirementsStruct[key], before.requirementsStruct[key])
  assert.deepEqual(after.preferences, before.preferences)
  await page.reload(); await page.getByRole('button', { name: '对话调整', exact: true }).waitFor()
  assert.deepEqual((await detail()).dayList, after.dayList)
  await page.screenshot({ path: resolve(out, '04-saved-and-refreshed.png'), fullPage: true })
  assert.equal(result.pageErrors.length, 0)
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error); process.exitCode = 1
  await page?.screenshot({ path: resolve(out, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  clearTimeout(timer); await Promise.all(pending); await context.close(); await browser.close(); save()
  console.log(JSON.stringify({ status: result.status, timing: result.timing, error: result.error }))
}
