// 正式页面三条旅程；验收条件先于运行定义，模型/资料回放不计理解准确率。
import assert from 'node:assert/strict'
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const scenario = process.argv[2]
assert(['plan', 'conflict', 'manual'].includes(scenario))
const out = resolve(process.env.SINAN_JOURNEY_OUTPUT)
mkdirSync(out, { recursive: true })
const base = 'http://127.0.0.1:15177'
const louvre = 'Musée du Louvre', orsay = "Musée d'Orsay"
const result = { scenario, mode: 'formal browser/API/queue/solver; frozen model boundary and external facts', steps: [], pageErrors: [], responses: [] }
const save = () => writeFileSync(resolve(out, 'browser.json'), JSON.stringify(result, null, 2))
const step = name => { result.steps.push(name); save() }
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 1080 } })
const pending = []
let page
const timer = setTimeout(async () => { result.status = 'budget_exhausted'; save(); await browser.close(); process.exit(2) }, 55_000)
try {
  await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
  await context.addInitScript(() => {
    window.__journeyPreviews = []
    new MutationObserver(() => {
      const node = document.querySelector('[aria-label="尚未排程的研究候选"]')
      if (node && !window.__journeyPreviews.includes(node.textContent)) window.__journeyPreviews.push(node.textContent)
    }).observe(document, { childList: true, subtree: true })
  })
  page = await context.newPage(); page.setDefaultTimeout(7000)
  page.on('pageerror', e => result.pageErrors.push(String(e)))
  page.on('response', response => {
    const path = new URL(response.url()).pathname
    if (/\/city\/clarify$|\/generate$|constraint-repairs|image-intent|replans|apply-plans/.test(path)) {
      pending.push(response.json().then(body => { result.responses.push({ path, http: response.status(), body }); save() }).catch(() => {}))
    }
  })
  await page.goto(`${base}/login`)
  await page.getByRole('button', { name: '还没有账号？创建一个' }).click()
  await page.getByLabel('怎么称呼你').fill('匿名旅程验收')
  await page.getByLabel('账号', { exact: true }).fill(`journey_${Date.now().toString(36)}`)
  await page.getByLabel('密码', { exact: true }).fill('journey-local-20261025')
  await page.getByRole('button', { name: '注册并登录', exact: true }).click()
  await page.waitForURL('**/trips'); await page.goto(`${base}/`)
  const common = '2026-10-25去Paris，一人一天不住宿，喜欢艺术；每天最多2个景点，预算200欧元硬上限，14:00以后开始，18:00前结束，不去Eiffel Tower。'
  const request = common + (scenario === 'manual' ? `必须去${orsay}。另有一份卢浮宫预约截图需要导入。` : `必须去${louvre}，必须去${orsay}。`) +
    (scenario === 'conflict' ? `第1天必须先去${louvre}再去${orsay}；第1天必须先去${orsay}再去${louvre}。` : '')
  result.rawInput = request
  await page.getByRole('textbox', { name: '说说你的旅行想法' }).fill(request)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.intake-confirm').waitFor()
  const options = page.locator('details').filter({ has: page.locator('summary', { hasText: '按时间要求安排' }) }).first()
  await options.locator('summary').click()
  await options.getByLabel('国家代码', { exact: true }).fill('FR')
  await options.getByLabel('当地时区', { exact: true }).fill('Europe/Paris')
  await options.getByLabel('计价币种', { exact: true }).fill('EUR')
  await options.getByRole('button', { name: '确认并启用时间约束规划', exact: true }).click()
  if (scenario === 'manual') {
    const section = page.locator('.intake-confirm .screenshot-import')
    await section.locator('summary').click()
    await section.getByLabel('截图类型').selectOption('booking')
    await section.locator('input[type=file]').setInputFiles(resolve('../travel-agent-python/tests/fixtures/screenshots/booking.png'))
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
    assert.equal(await section.getByLabel('结束当地日期').inputValue(), '')
    await section.getByLabel('结束当地日期').fill('2026-10-25')
    await section.locator('input[type=checkbox]').check()
    await section.getByRole('button', { name: '确认加入需求表单', exact: true }).click()
    await section.getByRole('status').filter({ hasText: '已生成待采用输入' }).waitFor()
    step('截图字段可框选；无召回→手工地址且坐标留空→补齐结束日期→明确采用预约')
    await page.screenshot({ path: resolve(out, '01-manual-confirmed.png'), fullPage: true })
  } else await page.screenshot({ path: resolve(out, '01-requirements.png'), fullPage: true })

  async function start() {
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/itinerary/generate' && r.request().method() === 'POST')
    await page.locator('.intake-start').click()
    const body = await (await response).json()
    assert(body.data?.id, JSON.stringify(body))
    return body.data.id
  }
  const detail = async id => {
    const response = await context.request.get(`${base}/api/itinerary/${id}`)
    assert.equal(response.status(), 200)
    return (await response.json()).data
  }
  let id = await start()
  if (scenario === 'conflict') {
    const repair = page.getByRole('region', { name: '调整冲突要求' })
    await repair.waitFor()
    const before = await detail(id)
    assert.equal(before.dayList[0].constraintPlanning.status, 'INFEASIBLE')
    assert(before.requirementsStruct.placeRelations.every(r => r.strength === 'hard'))
    const suggestion = before.dayList[0].constraintPlanning.repairSuggestions.find(s => s.constraintId === 'backward')
    assert(suggestion)
    assert(!(await page.locator('.trip-board').innerText()).includes('重新生成这一天'))
    assert((await page.getByLabel('预算小计').innerText()).includes('EUR'))
    await page.screenshot({ path: resolve(out, '02-conflict.png'), fullPage: true })
    await repair.getByRole('button', { name: `采用修订：${suggestion.label}`, exact: true }).click()
    await page.locator('.intake-confirm').waitFor()
    assert.deepEqual(await detail(id), before, '选择修订不能改写旧行程')
    result.original = before
    const oldId = id
    id = await start()
    assert.notEqual(id, oldId)
    step('冲突未静默放宽；用户选择反向关系软化；旧行程保留后重新生成')
  }
  await page.locator('.trip-board-cta').waitFor()
  result.previews = await page.evaluate(() => window.__journeyPreviews)
  if (scenario === 'plan') assert(result.previews.some(text => text.includes('Jardin du Luxembourg')), '可选候选应曾出现在未排程研究池')
  assert.equal(await page.getByRole('region', { name: '尚未排程的研究候选' }).count(), 0)
  await page.locator('.trip-board-cta').click(); await page.waitForURL(/\/trips\/\d+/)
  const before = await detail(id)
  result.initial = before
  const items = before.dayList.flatMap(d => d.items)
  assert.equal(before.genState, 'COMPLETED')
  assert.equal(items.length, 2)
  assert(items.some(p => p.poiName === orsay))
  assert(!items.some(p => p.poiName === 'Eiffel Tower' || p.poiName === 'Jardin du Luxembourg'))
  assert.equal(before.requirementsStruct.maxAttractionsPerDay, 2)
  assert.equal(before.requirementsStruct.budgetPolicy.mode, 'hard_cap')
  assert(items.every(p => p.costCurrency === 'EUR' && Number.isFinite(Number(p.cost))))
  assert(items.reduce((sum, p) => sum + Number(p.cost), 0) <= 200)
  const ordered = [...items].sort((a, b) => a.startTime.localeCompare(b.startTime))
  const minute = time => Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5))
  assert(minute(ordered[0].startTime) >= 14 * 60 && minute(ordered.at(-1).endTime) <= 18 * 60)
  assert(minute(ordered[0].endTime) + 10 <= minute(ordered[1].startTime))
  if (scenario === 'conflict') {
    assert.equal(ordered[0].poiName, louvre)
    assert.equal(before.requirementsStruct.placeRelations.find(r => r.constraintId === 'forward').strength, 'hard')
    assert.equal(before.requirementsStruct.placeRelations.find(r => r.constraintId === 'backward').strength, 'soft')
  }
  if (scenario === 'manual') {
    const place = before.requirementsStruct.placeInputs[0]
    assert.equal(place.identitySource, 'user_provided'); assert.equal(place.latitude, null)
    assert.equal(place.sourceUrl, null); assert.match(place.address, /金字塔入口/)
    const fixed = before.requirementsStruct.fixedArrangements[0]
    assert.equal(fixed.startTime, '14:00'); assert.equal(fixed.endTime, '16:00')
    const locked = items.find(p => p.poiName === fixed.name)
    assert.equal(locked.startTime.slice(0, 5), '14:00'); assert.equal(locked.endTime.slice(0, 5), '16:00')
    await page.getByRole('button', { name: '对话调整', exact: true }).click()
    const composer = page.getByRole('region', { name: '约束行程修改' })
    const target = items.find(p => p.poiName === orsay)
    await composer.getByLabel('要调整的安排').selectOption(String(target.id))
    await composer.getByLabel('用一句话描述').fill('推迟10分钟')
    await composer.getByRole('button', { name: '理解并比较', exact: true }).click()
    await page.getByRole('button', { name: '选此方案，查看差异', exact: true }).first().click()
    await page.getByRole('button', { name: '应用到行程', exact: true }).waitFor()
    assert.deepEqual((await detail(id)).dayList, before.dayList)
    await page.screenshot({ path: resolve(out, '02-diff.png'), fullPage: true })
    await Promise.all([
      page.waitForResponse(r => /apply-plans$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST'),
      page.getByRole('button', { name: '应用到行程', exact: true }).click(),
    ])
    const after = await detail(id)
    result.final = after
    assert.deepEqual(after.requirementsStruct.fixedArrangements, before.requirementsStruct.fixedArrangements)
    const afterItems = after.dayList.flatMap(d => d.items)
    const retained = afterItems.find(p => p.id === locked.id)
    for (const key of ['id', 'poiName', 'startTime', 'endTime', 'latitude', 'longitude', 'cost', 'costCurrency']) assert.equal(retained[key], locked[key])
    assert(minute(afterItems.find(p => p.id === target.id).startTime) >= minute(target.startTime) + 10)
    assert.equal(after.planningRevision, before.planningRevision + 1)
    step('延误→比较→Diff；确认前未变，确认后只推进一次修订并保留原预约')
  } else step('最终安排符合原话边界，落选候选不残留，无唯一顺序/开始时间断言')
  await page.reload(); await page.getByRole('button', { name: '对话调整', exact: true }).waitFor()
  assert.deepEqual((await detail(id)).dayList, (result.final || before).dayList)
  await page.screenshot({ path: resolve(out, '03-final.png'), fullPage: true })
  assert.deepEqual(result.pageErrors, [])
  result.status = 'passed'
} catch (error) {
  result.status = 'failed'; result.error = String(error.stack || error); process.exitCode = 1
  result.visibleText = await page?.locator('body').innerText().catch(() => '')
  await page?.screenshot({ path: resolve(out, 'failure.png'), fullPage: true }).catch(() => {})
} finally {
  clearTimeout(timer); await Promise.allSettled(pending); await context.close(); await browser.close(); save()
  console.log(JSON.stringify({ scenario, status: result.status, steps: result.steps, error: result.error }))
}
