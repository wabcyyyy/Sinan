// 正式页面/API/鉴权/数据库/CP-SAT；仅研究与外部取证固定，禁止新模型及公网。
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const base = 'http://127.0.0.1:15174'
const out = resolve(process.env.P4_RUN_DIR, `browser-${Date.now()}`)
mkdirSync(out, { recursive: true })
const started = Date.now()
const result = { startedAt: new Date().toISOString(), steps: [], responses: [], pageErrors: [], mode: 'real UI/API/auth/SQLite/worker/CP-SAT; frozen research and source observations; no model/network' }
let browser, context, page
const pending = []
const save = () => writeFileSync(resolve(out, 'p4-browser.json'), JSON.stringify(result, null, 2))
const step = name => { result.steps.push({ name, ms: Date.now() - started }); console.log(name); save() }
const detail = async id => (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
const budget = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser?.close(); process.exit(2) }, 40_000)

try {
  browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
  context = await browser.newContext({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 1360, height: 1000 } })
  await context.route('**/*', route => ['127.0.0.1', 'localhost', ''].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  page = await context.newPage(); page.setDefaultTimeout(7_000); page.setDefaultNavigationTimeout(7_000)
  page.on('pageerror', error => result.pageErrors.push(String(error)))
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/\/itinerary\/generate$|\/replans\/(compare|select)$|\/apply-plans$/.test(path)) {
      pending.push(response.json().then(body => result.responses.push({ path, status: response.status(), body })).catch(() => {}))
    }
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('P4 匿名验收')
  await page.getByLabel('账号', { exact: true }).fill(`p4_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('p4-browser-pass-123')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips')
  step('registered and authenticated with real cookie')
  await page.goto(`${base}/`)
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill('Paris 一天 一人 2026-10-25 预算100')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor({ state: 'visible' })
  const settings = page.locator('details').filter({ has: page.locator('summary', { hasText: '按时间要求安排' }) })
  await settings.locator('summary').click()
  await settings.getByLabel('国家代码', { exact: true }).fill('FR')
  await settings.getByLabel('当地时区', { exact: true }).fill('Europe/Paris')
  await settings.getByLabel('计价币种', { exact: true }).fill('EUR')
  const enabled = settings.getByLabel('优先核实影响安排的资料', { exact: true })
  assert.equal(await enabled.isChecked(), false)
  await enabled.check()
  assert.equal(await settings.getByLabel('允许联网来源搜索（可能消耗 AI 额度）', { exact: true }).isChecked(), false)
  await settings.getByRole('button', { name: '确认并启用时间约束规划', exact: true }).click()
  await page.locator('.intake-start').click()
  await page.locator('.trip-board-cta').waitFor({ state: 'visible', timeout: 12_000 })
  await page.locator('.trip-board-cta').click()
  await page.waitForURL(/\/trips\/\d+/)
  const id = Number(page.url().match(/\/trips\/(\d+)/)[1])
  const before = await detail(id)
  assert.equal(before.genState, 'COMPLETED')
  assert.equal(before.dayList[0].items.length, 2)
  const active = before.dayList[0].constraintPlanning.activeEvidence
  assert.equal(active.stopReason, 'awaiting_user'); assert(active.questions.some(q => q.includes('A 的费用')))
  assert.equal(before.requirementsStruct.constraintPlanning.activeEvidence.allowSourceSearch, false)
  const notice = page.locator('[aria-label="时间约束规划说明"] details').filter({ has: page.locator('summary', { hasText: '本轮资料核实' }) })
  await notice.locator('summary').click()
  assert.match(await notice.innerText(), /A 的费用仍待确认/)
  await page.screenshot({ path: resolve(out, 'created-evidence.png'), fullPage: true })
  step('UI opt-in → reconfirm → real worker/solver → source updates and unresolved question displayed')
  await page.reload()
  await page.getByRole('button', { name: '对话调整', exact: true }).waitFor({ state: 'visible' })
  assert.deepEqual((await detail(id)).dayList, before.dayList)
  await page.getByRole('button', { name: '对话调整', exact: true }).click()
  const composer = page.getByRole('region', { name: '约束行程修改' })
  await composer.locator('summary', { hasText: '或手动填写变化与保留范围' }).click()
  await composer.getByLabel('变化类型').selectOption('finish_by')
  await composer.getByLabel('结束／截止时间', { exact: true }).fill('13:45')
  const compared = page.waitForResponse(r => r.url().endsWith(`/itinerary/${id}/replans/compare`))
  await composer.getByRole('button', { name: '比较修改方案', exact: true }).click()
  const comparedResponse = await compared
  assert.equal(comparedResponse.status(), 200)
  const comparison = (await comparedResponse.json()).data
  assert(comparison.activeEvidence.queries.some(q => q.outcome === 'updated'))
  assert.deepEqual(await detail(id), before)
  await composer.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().click()
  await page.getByRole('region', { name: '局部修改预览' }).waitFor({ state: 'visible' })
  assert.deepEqual(await detail(id), before)
  const diffText = await page.locator('.draft-review-changes').innerText()
  assert.match(diffText, /EUR/)
  assert(!/[¥￥]/.test(diffText), 'EUR 费用差异不可被显示为人民币')
  // 桌面工作区同步显示 DraftReview，也必须复用同一原币种差异。
  assert.match(await page.getByRole('region', { name: '局部修改预览' }).innerText(), /EUR/)
  await page.screenshot({ path: resolve(out, 'replan-draft.png'), fullPage: true })
  step('P2 compare performs active evidence → server draft/Diff; formal trip unchanged')
  const applied = page.waitForResponse(r => r.url().endsWith(`/itinerary/${id}/apply-plans`))
  await page.getByRole('button', { name: '应用到行程', exact: true }).click()
  const appliedResponse = await applied
  assert.equal(appliedResponse.status(), 200)
  const after = (await appliedResponse.json()).data
  assert.equal(after.planningRevision, before.planningRevision + 1)
  const a = after.dayList[0].items.find(p => p.poiName === 'A')
  assert.equal(a.cost, 15); assert.equal(a.startTime.slice(0, 5), '09:00'); assert.equal(a.endTime.slice(0, 5), '11:00')
  assert.equal(a.factEvidence.cost.source_url, 'https://example.com/frozen-p4')
  assert.deepEqual(after.dayList[0].constraintPlanning.activeEvidence, comparison.activeEvidence)
  await page.reload()
  await page.getByRole('button', { name: '对话调整', exact: true }).waitFor({ state: 'visible' })
  assert.deepEqual((await detail(id)).dayList, after.dayList)
  await page.screenshot({ path: resolve(out, 'saved-refresh.png'), fullPage: true })
  step('explicit UI confirmation → CAS save → source/cost/locked times survive refresh')
  const stale = await context.request.post(`${base}/api/itinerary/${id}/replans/compare`, { data: {
    planningRevision: before.planningRevision, change: { kind: 'finish_by', dayNo: 1, endTime: '13:45' },
  } })
  assert.equal(stale.status(), 409)
  result.before = before; result.comparison = comparison; result.after = after; result.staleStatus = stale.status()
  const loggedOut = page.waitForResponse(r => r.url().endsWith('/api/auth/logout'))
  await page.getByRole('button', { name: '登出', exact: true }).click(); await loggedOut
  assert.equal((await context.request.get(`${base}/api/itinerary/${id}`)).status(), 401)
  result.logoutStatus = 401
  assert.deepEqual(result.pageErrors, [])
  step('stale revision rejected; real logout removes access')
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error.stack || error); process.exitCode = 1
  if (page) {
    result.visibleText = (await page.locator('body').innerText().catch(() => '')).slice(-14_000)
    await page.screenshot({ path: resolve(out, 'failure.png'), fullPage: true }).catch(() => {})
  }
  console.error(result.error)
} finally {
  clearTimeout(budget); await Promise.allSettled(pending)
  result.seconds = (Date.now() - started) / 1000; save(); await browser?.close()
  console.log(JSON.stringify({ status: result.status, seconds: result.seconds, output: resolve(out, 'p4-browser.json') }))
}
