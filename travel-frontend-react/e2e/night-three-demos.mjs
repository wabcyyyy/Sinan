// 正式页面三段连续演示，源模型/外部资料明确回放，数据库与交互真实。
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const out = resolve(process.env.SINAN_NIGHT_BROWSER_DIR)
mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:15175'
const result = { method: 'formal UI/API/auth/SQLite/worker/solver; recorded real intake; controller form; synthetic POI/routes; no new models', steps: [], responses: [], pageErrors: [], blockedRequests: [] }
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
let context, page
const pending = []
const started = Date.now()
const save = () => writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result, null, 2))
const step = name => { result.steps.push({ name, elapsedMs: Date.now() - started }); save() }
const budgetMs = Number(process.env.SINAN_NIGHT_BROWSER_BUDGET_MS || 50000)
assert(budgetMs > 0 && budgetMs <= 80000)
result.budgetMs = budgetMs
const timer = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser.close(); process.exit(2) }, budgetMs)
try {
  context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1080 }, recordVideo: { dir: resolve(out, 'video') } })
  await context.route('**/*', route => {
    const url = new URL(route.request().url())
    if (['127.0.0.1', 'localhost', ''].includes(url.hostname)) return route.continue()
    result.blockedRequests.push(url.origin); return route.abort()
  })
  page = await context.newPage(); page.setDefaultTimeout(5000)
  page.on('pageerror', e => result.pageErrors.push(String(e)))
  page.on('dialog', d => d.accept())
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/replans|apply-plans|\/generate$|\/city\/clarify$/.test(path)) pending.push(response.json().then(body => {
      result.responses.push({ path, status: response.status(), body })
    }).catch(() => {}))
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('匿名三段演示')
  await page.getByLabel('账号', { exact: true }).fill(`night_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('night-local-demo-123')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips')
  await page.goto(`${base}/`)
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill('2026年11月3日去Paris玩2天，1人不住宿，1000欧元硬上限，每天最多3个景点，09:00以后开始、18:00前结束。')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor({ state: 'visible' })
  await page.screenshot({ path: resolve(out, '01-confirm-complex-requirements.png'), fullPage: true })
  await page.locator('.intake-start').click()
  await page.locator('.trip-board-cta').waitFor({ state: 'visible', timeout: 8000 })
  await page.locator('.trip-board-cta').click()
  await page.waitForURL(/\/trips\/\d+/)
  const id = Number(page.url().match(/\/trips\/(\d+)/)[1]); result.itineraryId = id
  const detail = async () => (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
  const before = await detail(); result.initial = before
  assert.equal(before.genState, 'COMPLETED')
  assert.deepEqual(before.dayList.map(d => d.items.length), [3, 3])
  assert.equal(before.requirementsStruct.fixedArrangements[0].name, '地点0')
  assert.equal(before.requirementsStruct.placeRelations[0].kind, 'before')
  await page.screenshot({ path: resolve(out, '02-complex-created.png'), fullPage: true })
  step('demo 1: confirmed complex requirements -> worker -> six persisted items across two days')
  await page.getByRole('button', { name: '对话调整', exact: true }).click()
  const composer = page.getByRole('region', { name: '约束行程修改' })
  const item = name => before.dayList.flatMap(d => d.items).find(p => p.poiName === name)
  await composer.getByLabel('要调整的安排').selectOption(String(item('地点5').id))
  await composer.getByLabel('用一句话描述').fill('把地点5移到第1天15:00。')
  await composer.getByRole('button', { name: '理解并比较', exact: true }).click()
  await composer.getByRole('alert').filter({ hasText: '当前要求下未找到可行方案' }).waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await page.screenshot({ path: resolve(out, '03-conflict-preserves-original.png'), fullPage: true })
  step('demo 2: move conflicts with day-2 before relation -> explanation -> original database unchanged')
  await composer.getByLabel('要调整的安排').selectOption(String(item('地点2').id))
  await composer.getByLabel('用一句话描述').fill('把地点2的预约改到第1天12:00开始，第1天13:30前结束。')
  await composer.getByRole('button', { name: '理解并比较', exact: true }).click()
  await composer.getByRole('alert').waitFor()
  step('unsupported appointment phrasing -> explicit form confirmation, counted separately')
  await composer.locator('summary', { hasText: '或手动填写变化与保留范围' }).click()
  await composer.getByLabel('变化类型').selectOption('appointment')
  await composer.getByLabel('目标日期').selectOption('1')
  await composer.getByLabel('开始时间', { exact: true }).fill('12:00')
  await composer.getByLabel('结束／截止时间', { exact: true }).fill('13:30')
  await composer.getByRole('button', { name: '比较修改方案', exact: true }).click()
  await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await page.screenshot({ path: resolve(out, '04-local-comparison.png'), fullPage: true })
  await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().click()
  await page.getByRole('button', { name: '应用到行程', exact: true }).waitFor()
  assert.deepEqual((await detail()).dayList, before.dayList)
  await Promise.all([
    page.waitForResponse(r => /apply-plans$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST'),
    page.getByRole('button', { name: '应用到行程', exact: true }).click(),
  ])
  const after = await detail(); result.final = after
  assert(after.planningRevision > before.planningRevision)
  const appointment = after.dayList[0].items.find(p => p.poiName === '地点2')
  assert.equal(appointment.startTime.slice(0, 5), '12:00'); assert.equal(appointment.endTime.slice(0, 5), '13:30')
  assert(after.requirementsStruct.fixedArrangements.some(f => f.name === '地点2'))
  await page.reload()
  await page.getByRole('button', { name: '对话调整', exact: true }).waitFor()
  assert.deepEqual((await detail()).dayList, after.dayList)
  assert.equal(result.pageErrors.length, 0)
  await page.screenshot({ path: resolve(out, '05-confirmed-and-refreshed.png'), fullPage: true })
  step('demo 3: scoped replan -> select draft (unchanged) -> apply -> persisted appointment and refresh match')
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error); process.exitCode = 1
  await page?.screenshot({ path: resolve(out, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  clearTimeout(timer); await Promise.all(pending); await context?.close(); await browser.close(); save()
  console.log(JSON.stringify({ status: result.status, steps: result.steps, error: result.error }))
}
