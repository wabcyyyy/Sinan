import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const out = resolve(process.env.SINAN_NIGHT26_LIVE_OUTPUT)
mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:15178'
const result = { scope: 'current real NL and public named sources; formal UI/auth/worker/SQLite; conditional facts remain UNKNOWN', responses: [], pageErrors: [] }
const save = () => writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result, null, 2))
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1080 }, recordVideo: { dir: resolve(out, 'video') } })
const pending = []
let page
const timer = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser.close(); process.exit(2) }, 65000)
try {
  await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  page = await context.newPage(); page.setDefaultTimeout(7000)
  page.on('pageerror', error => result.pageErrors.push(String(error)))
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/\/itinerary\/clarify$|\/generate$|replans|apply-plans/.test(path)) pending.push(response.json().then(body => { result.responses.push({ path, http: response.status(), body }); save() }).catch(() => {}))
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('匿名本夜连续验收')
  await page.getByLabel('账号', { exact: true }).fill(`night26_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('anonymous-local-night26')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips'); await page.goto(`${base}/`)
  const raw = '2026年11月4日去Paris，一人一天，不住宿不排餐饮；当天14:00以后开始，18:00前结束。必须去Musée du Louvre（卢浮宫）和Musée d’Orsay（奥赛博物馆），每天最多2个景点，不去Eiffel Tower。活动门票预算100欧元硬上限，不含城际食宿。喜欢艺术。每馆计划90分钟，先去哪家都可以。'
  result.rawInput = raw + ' 本次为原话来源锚点修复后的必要回归。'
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill(result.rawInput)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor({ timeout: 16000 })
  const planner = page.locator('details').filter({ has: page.locator('summary', { hasText: '按时间要求安排' }) }).first()
  await planner.locator('summary').click()
  await planner.getByLabel('国家代码').fill('FR')
  await planner.getByLabel('当地时区').fill('Europe/Paris')
  await planner.getByLabel('计价币种').fill('EUR')
  await planner.getByRole('button', { name: /确认并启用时间约束规划|更新目的地口径/ }).click()
  await page.screenshot({ path: resolve(out, '01-confirmation.png'), fullPage: true })
  await page.locator('.intake-start').click()
  await page.locator('.trip-board-cta').waitFor({ timeout: 30000 })
  await page.locator('.trip-board-cta').click(); await page.waitForURL(/\/trips\/\d+/)
  const id = Number(page.url().match(/\/trips\/(\d+)/)[1]); result.itineraryId = id
  const detail = async () => (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
  const before = await detail(); result.initial = before; save()
  assert.equal(before.genState, 'COMPLETED')
  assert.equal(before.dayList.flatMap(day => day.items).length, 2)
  assert.equal(before.requirementsStruct.budgetPolicy.mode, 'hard_cap')
  assert.equal(before.requirementsStruct.maxAttractionsPerDay, 2)
  assert.equal(before.dayList[0].constraintPlanning.status, 'UNKNOWN')
  assert(before.requirementsStruct.requiredPlaces.length >= 2)
  await page.screenshot({ path: resolve(out, '02-created-unknown.png'), fullPage: true })
  await page.getByRole('button', { name: '对话调整', exact: true }).click()
  const composer = page.getByRole('region', { name: '约束行程修改' })
  const target = before.dayList.flatMap(day => day.items).find(item => /orsay|奥赛/i.test(item.poiName))
  assert(target)
  await composer.getByLabel('要调整的安排').selectOption(String(target.id))
  await composer.locator('summary', { hasText: '或手动填写变化与保留范围' }).click()
  await composer.getByLabel('变化类型').selectOption('appointment')
  await composer.getByLabel('目标日期').selectOption('1')
  await composer.getByLabel('开始时间', { exact: true }).fill('16:30')
  await composer.getByLabel('结束／截止时间', { exact: true }).fill('18:00')
  await composer.getByRole('button', { name: '比较修改方案', exact: true }).click()
  await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().click()
  await page.getByRole('button', { name: '应用到行程', exact: true }).waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await page.screenshot({ path: resolve(out, '03-diff-before-confirmation.png'), fullPage: true })
  await Promise.all([page.waitForResponse(r => /apply-plans$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST'), page.getByRole('button', { name: '应用到行程', exact: true }).click()])
  const after = await detail(); result.final = after
  assert(after.requirementsStruct.fixedArrangements.some(f => f.name === target.poiName && f.startTime === '16:30' && f.endTime === '18:00'))
  for (const key of ['requiredPlaces', 'excludedPlaces', 'budgetPolicy']) assert.deepEqual(after.requirementsStruct[key], before.requirementsStruct[key])
  await page.reload(); await page.getByRole('button', { name: '对话调整', exact: true }).waitFor()
  assert.deepEqual((await detail()).dayList, after.dayList)
  await page.screenshot({ path: resolve(out, '04-saved-refreshed.png'), fullPage: true })
  assert.deepEqual(result.pageErrors, [])
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error.stack || error); process.exitCode = 1
  result.visibleText = await page?.locator('body').innerText().catch(() => '')
  await page?.screenshot({ path: resolve(out, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  clearTimeout(timer); await Promise.allSettled(pending); await context.close(); await browser.close(); save()
  console.log(JSON.stringify({ status: result.status, error: result.error }))
}
