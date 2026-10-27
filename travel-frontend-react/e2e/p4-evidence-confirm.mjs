// 续接已由页面创建/比较/选定的服务端草稿；重新登录后只验确认、刷新与币种。
import assert from 'node:assert/strict'
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const priorPath = process.env.P4_RESUME_FILE
const prior = JSON.parse(readFileSync(priorPath, 'utf8'))
const account = prior.visibleText.match(/\bp4_[a-z0-9]+\b/)[0]
const id = prior.responses.find(r => r.path.endsWith('/itinerary/generate')).body.data.id
const comparison = prior.responses.find(r => r.path.endsWith('/replans/compare')).body.data
const out = resolve(process.env.P4_RUN_DIR, `confirm-${Date.now()}`)
mkdirSync(out, { recursive: true })
const started = Date.now()
const result = { resumes: priorPath, tripId: id, steps: [], pageErrors: [] }
const base = 'http://127.0.0.1:15174'
let browser, page
const budget = setTimeout(async () => { result.status = 'budget_exhausted'; await browser?.close(); process.exit(2) }, 10_000)
try {
  browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
  const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1360, height: 1000 } })
  await context.route('**/*', route => ['127.0.0.1', 'localhost', ''].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  page = await context.newPage(); page.setDefaultTimeout(3000); page.setDefaultNavigationTimeout(3000)
  page.on('pageerror', error => result.pageErrors.push(String(error)))
  await page.goto(`${base}/login`)
  await page.getByLabel('账号', { exact: true }).fill(account)
  await page.getByLabel('密码', { exact: true }).fill('p4-browser-pass-123')
  await page.getByRole('button', { name: '登录司南', exact: true }).click(); await page.waitForURL('**/trips')
  const detail = async () => (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
  const before = await detail()
  assert.equal(before.dayList[0].items.find(p => p.poiName === 'A').cost, 10)
  assert.equal(before.planningRevision, comparison.planningRevision)
  await page.goto(`${base}/trips/${id}`)
  await page.getByRole('button', { name: '待确认修改', exact: true }).click()
  const review = page.getByRole('region', { name: '局部修改预览' })
  const text = await review.innerText()
  assert.match(text, /EUR\s*10\.00→EUR\s*15\.00/)
  assert(!/[¥￥]/.test(text))
  result.steps.push('relogin restores pending server draft; explicit EUR diff; original cost still 10')
  await page.screenshot({ path: resolve(out, 'eur-draft.png'), fullPage: true })
  const applied = page.waitForResponse(r => r.url().endsWith(`/itinerary/${id}/apply-plans`))
  await review.getByRole('button', { name: '应用到行程', exact: true }).click()
  const response = await applied; assert.equal(response.status(), 200)
  const after = (await response.json()).data
  const a = after.dayList[0].items.find(p => p.poiName === 'A')
  assert.equal(a.cost, 15); assert.equal(a.costCurrency, 'EUR')
  assert.equal(a.startTime.slice(0, 5), '09:00'); assert.equal(a.endTime.slice(0, 5), '11:00')
  assert.equal(a.factEvidence.cost.source_url, 'https://example.com/frozen-p4')
  assert.equal(after.planningRevision, before.planningRevision + 1)
  assert.deepEqual(after.dayList[0].constraintPlanning.activeEvidence, comparison.activeEvidence)
  await page.reload(); await page.getByRole('button', { name: '对话调整', exact: true }).waitFor({ state: 'visible' })
  assert.deepEqual((await detail()).dayList, after.dayList)
  const stale = await context.request.post(`${base}/api/itinerary/${id}/replans/compare`, { data: { planningRevision: before.planningRevision, change: { kind: 'finish_by', dayNo: 1, endTime: '13:45' } } })
  assert.equal(stale.status(), 409)
  const logout = page.waitForResponse(r => r.url().endsWith('/api/auth/logout'))
  await page.getByRole('button', { name: '登出', exact: true }).click(); await logout
  assert.equal((await context.request.get(`${base}/api/itinerary/${id}`)).status(), 401)
  result.steps.push('UI confirms CAS; evidence/cost/EUR/locked times survive refresh; stale 409 and logout 401')
  assert.deepEqual(result.pageErrors, [])
  result.before = before; result.after = after; result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error.stack || error); process.exitCode = 1
} finally {
  clearTimeout(budget); result.seconds = (Date.now() - started) / 1000
  writeFileSync(resolve(out, 'p4-confirmation.json'), JSON.stringify(result, null, 2))
  await browser?.close()
  console.log(JSON.stringify({ status: result.status, seconds: result.seconds, output: resolve(out, 'p4-confirmation.json') }))
}
