import assert from 'node:assert/strict'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const root = resolve(process.env.SINAN_NIGHT26_REPLAY_ROOT)
const scenario = JSON.parse(readFileSync(resolve(root, 'scenario.json'), 'utf8'))
const out = resolve(root, 'frontend'); mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:15178'
const result = { scenario: scenario.scenario, mode: scenario.mode, steps: [], responses: [], pageErrors: [] }
const save = () => writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result, null, 2))
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1080 }, recordVideo: { dir: resolve(out, 'video') } })
let page; const pending = []
const timer = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser.close(); process.exit(2) }, 50000)
try {
  await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  page = await context.newPage(); page.setDefaultTimeout(7000)
  page.on('pageerror', error => result.pageErrors.push(String(error)))
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/clarify|generate|constraint-repairs|image-intent|replans|apply-plans/.test(path)) pending.push(response.json().then(body => { result.responses.push({ path, http: response.status(), body }); save() }).catch(() => {}))
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('匿名本夜回放验收')
  await page.getByLabel('账号', { exact: true }).fill(`replay26_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('anonymous-local-night26')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips'); await page.goto(`${base}/`)
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill(scenario.raw)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor()
  if (scenario.scenario === 'screenshot') {
    const section = page.locator('.intake-confirm .screenshot-import')
    await section.locator('summary').click()
    await section.getByLabel('截图类型').selectOption('booking')
    await section.locator('input[type=file]').setInputFiles(scenario.image)
    await section.getByRole('button', { name: '识别截图字段', exact: true }).click()
    await section.getByRole('button', { name: /^name：/ }).click()
    await section.locator('[aria-label="当前字段证据区域"]').waitFor()
    await section.getByRole('button', { name: '查询地点实体', exact: true }).click()
    await section.getByText('手工补充地点', { exact: true }).waitFor()
    assert.equal(await section.locator('input[type=radio]').count(), 0)
    await section.getByLabel('手工地址').fill('用户核对：巴黎卢浮宫金字塔入口')
    await section.getByLabel('地点类别').selectOption('museum')
    await section.getByRole('button', { name: '登记手工地点', exact: true }).click()
    await section.getByText(/手工输入，未核验/).waitFor()
    assert(await section.getByRole('button', { name: '确认加入需求表单', exact: true }).isDisabled())
    await section.locator('input[type=radio]').check()
    assert.equal(await section.getByLabel('结束当地日期').inputValue(), '2026-11-04')
    assert.equal(Number(await section.getByLabel('原币金额').inputValue()), 0)
    await section.locator('input[type=checkbox]').check()
    await section.getByRole('button', { name: '确认加入需求表单', exact: true }).click()
    await section.getByRole('status').filter({ hasText: '已生成待采用输入' }).waitFor()
    result.steps.push('new anonymous screenshot replay -> crop -> no automatic entity -> explicit manual address -> zero amount confirmation')
  }
  const start = async () => {
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/itinerary/generate' && r.request().method() === 'POST')
    await page.locator('.intake-start').click()
    const body = await (await response).json(); assert(body.data?.id, JSON.stringify(body)); return body.data.id
  }
  const detail = async id => (await (await context.request.get(`${base}/api/itinerary/${id}`)).json()).data
  let id = await start()
  if (scenario.scenario === 'conflict') {
    const repair = page.getByRole('region', { name: '调整冲突要求' }); await repair.waitFor()
    const before = await detail(id); result.original = before
    assert.equal(before.dayList[0].constraintPlanning.status, 'INFEASIBLE')
    assert(before.requirementsStruct.placeRelations.every(r => r.strength === 'hard'))
    const suggestion = before.dayList[0].constraintPlanning.repairSuggestions.find(s => s.constraintId === scenario.reverse_id)
    assert(suggestion)
    await page.screenshot({ path: resolve(out, '01-conflict.png'), fullPage: true })
    await repair.getByRole('button', { name: `采用修订：${suggestion.label}`, exact: true }).click()
    await page.locator('.intake-confirm').waitFor()
    assert.deepEqual(await detail(id), before)
    const old = id; id = await start(); assert.notEqual(id, old)
    result.steps.push('both hard relations retained -> script chooses server repair -> old itinerary unchanged -> new solve')
  }
  await page.locator('.trip-board-cta').waitFor({ timeout: 12000 })
  await page.locator('.trip-board-cta').click(); await page.waitForURL(/\/trips\/\d+/)
  const before = await detail(id); result.initial = before; result.itineraryId = id; save()
  assert.equal(before.genState, 'COMPLETED')
  if (scenario.scenario === 'conflict') {
    assert.equal(before.dayList.flatMap(d => d.items).length, 3)
    assert.equal(before.requirementsStruct.placeRelations.find(r => r.constraintId === scenario.reverse_id).strength, 'soft')
    assert.equal(before.requirementsStruct.placeRelations.filter(r => r.strength === 'hard').length, 1)
    for (const key of ['requiredPlaces', 'dayWindows', 'budgetPolicy', 'fixedArrangements']) assert.deepEqual(before.requirementsStruct[key], result.original.requirementsStruct[key])
  } else {
    const items = before.dayList.flatMap(d => d.items)
    assert.equal(items.length, 2)
    const fixed = before.requirementsStruct.fixedArrangements.find(f => /Louvre/.test(f.name)); assert(fixed)
    assert.equal(fixed.startTime, '14:00'); assert.equal(fixed.endTime, '15:30')
    assert.equal(Number(fixed.cost), 0)
    const place = before.requirementsStruct.placeInputs[0]
    assert.equal(place.identitySource, 'user_provided'); assert.equal(place.latitude, null)
    const chosen = items.find(p => p.poiName === fixed.name)
    assert.equal(before.dayList[0].constraintPlanning.facts.find(f => f.candidateId === chosen.poiId).sourceEntityId, place.entityId)
    await page.getByRole('button', { name: '对话调整', exact: true }).click()
    const composer = page.getByRole('region', { name: '约束行程修改' })
    const target = items.find(p => /orsay|奥赛/i.test(p.poiName)); assert(target)
    await composer.getByLabel('要调整的安排').selectOption(String(target.id))
    await composer.locator('summary', { hasText: '或手动填写变化与保留范围' }).click()
    await composer.getByLabel('变化类型').selectOption('delay')
    await composer.getByLabel('推迟分钟').fill('10')
    await composer.getByRole('button', { name: '比较修改方案', exact: true }).click()
    await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().click()
    await page.getByRole('button', { name: '应用到行程', exact: true }).waitFor()
    assert.deepEqual((await detail(id)).dayList, before.dayList)
    await page.screenshot({ path: resolve(out, '02-diff.png'), fullPage: true })
    await Promise.all([page.waitForResponse(r => /apply-plans$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST'), page.getByRole('button', { name: '应用到行程', exact: true }).click()])
    const after = await detail(id); result.final = after
    assert.deepEqual(after.requirementsStruct.fixedArrangements, before.requirementsStruct.fixedArrangements)
    const locked = items.find(p => p.poiName === fixed.name)
    const retained = after.dayList.flatMap(d => d.items).find(p => p.id === locked.id)
    for (const key of ['id', 'poiName', 'startTime', 'endTime', 'cost', 'costCurrency']) assert.equal(retained[key], locked[key])
    assert.equal(after.planningRevision, before.planningRevision + 1)
    result.steps.push('delay -> compare -> Diff -> explicit apply; locked screenshot reservation and zero amount retained')
  }
  await page.reload(); await page.getByRole('button', { name: '对话调整', exact: true }).waitFor()
  assert.deepEqual((await detail(id)).dayList, (result.final || before).dayList)
  await page.screenshot({ path: resolve(out, '03-final.png'), fullPage: true })
  assert.deepEqual(result.pageErrors, []); result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error.stack || error); process.exitCode = 1
  result.visibleText = await page?.locator('body').innerText().catch(() => '')
  await page?.screenshot({ path: resolve(out, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  clearTimeout(timer); await Promise.allSettled(pending); await context.close(); await browser.close(); save()
  console.log(JSON.stringify({ scenario: scenario.scenario, status: result.status, error: result.error }))
}
