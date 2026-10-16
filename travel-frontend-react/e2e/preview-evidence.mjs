// Existing Playwright + live probe report directory. Default: fully offline API replay.
// --live explicitly enables the real business generate endpoint; clarify stays frozen.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import pw from 'playwright-core'
import { installPreviewTiming } from './preview-timing.mjs'

const backend = fileURLToPath(new URL('../../travel-agent-python/', import.meta.url))
const casesPath = process.env.E2E_CASES || `${backend}/tests/agent_eval/live_pilot_cases.json`
const casesBytes = readFileSync(casesPath)
const cases = JSON.parse(casesBytes)
const live = process.argv.includes('--live')
const repeats = Number(process.env.E2E_REPEATS || 1)
assert.ok(Number.isInteger(repeats) && repeats > 0)
const base = process.env.E2E_BASE_URL || 'http://localhost:5173'
const channel = process.env.E2E_CHANNEL || 'msedge'
const timeout = Number(process.env.E2E_GEN_TIMEOUT_MS || (live ? 300_000 : 15_000))
const python = process.env.E2E_PYTHON || `${backend}/.venv/Scripts/python.exe`
const identity = JSON.parse(execFileSync(python, ['scripts/live_evidence.py'], { cwd: backend, encoding: 'utf8' }))
const report = {
  mode: live ? 'live-business-browser' : 'offline-replay-business-browser',
  identity, dataset_sha256: createHash('sha256').update(casesBytes).digest('hex'),
  started_at: new Date().toISOString(),
  scope: 'Frozen clarify; real UI submit/render. Offline mode intercepts every API and blocks external resources.',
  browser: { channel, viewport: { width: 1440, height: 1000 }, repeats, concurrency: 1, cache: 'uncontrolled' },
  runs: [],
}
const out = process.env.E2E_EVIDENCE_OUT || `${backend}/tests/agent_eval/report/live/${live ? 'pilot' : 'collection-smoke'}-${Date.now()}.json`
mkdirSync(fileURLToPath(new URL('../../travel-agent-python/tests/agent_eval/report/live/', import.meta.url)), { recursive: true })
const save = () => writeFileSync(out, JSON.stringify(report, null, 2) + '\n')
writeFileSync(out, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' })

function installOfflineFixture({ scenario, failure, regression }) {
  const response = (data) => new Response(JSON.stringify({ code: 200, message: 'ok', data }), { headers: { 'Content-Type': 'application/json' } })
  const item = (name, start = '10:00', end = '11:00') => ({ itemType: 'attraction', poiName: name, startTime: start, endTime: end, durationMin: 60, cost: 20 })
  let detail = {
    id: 701, city: scenario.slots.city, days: scenario.slots.days, persons: scenario.slots.persons,
    title: '离线采集回放', status: 1, genState: 'GENERATING', coreReady: false,
    dayList: Array.from({ length: scenario.slots.days }, (_, index) => ({
      id: index + 1, dayNo: index + 1, theme: '当天安排', generationStatus: 'PENDING', items: [],
    })), budget: 3000, budgetList: [], totalAmount: 100, stayNights: scenario.slots.days - 1, suggestions: [],
  }
  const original = window.fetch.bind(window)
  window.fetch = async (input, init) => {
    const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname
    if (!path.startsWith('/api/')) return original(input, init)
    if (path === '/api/user/info') return response({ id: 1, username: 'offline-evidence' })
    if (path === '/api/itinerary/clarify') return response({ slots: scenario.slots, state: { requirements: scenario.requirements }, ready: true, blocked: false, options: [], reply: '输入已冻结' })
    if (path === '/api/itinerary/supported-cities') return response(['杭州', '西安'])
    if (path === '/api/itinerary/generate' || path === '/api/itinerary/701') return response(detail)
    if (path === '/api/itinerary/701/events') {
      const encoder = new TextEncoder()
      return new Response(new ReadableStream({ start(controller) {
        const send = (type, data = {}) => controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type, itineraryId: 701, data })}\n\n`))
        setTimeout(() => send('item_preview', { runId: 'offline', previewId: 'offline:1:1', dayNo: 1, itemOrdinal: 1, item: { itemType: 'attraction', poiName: '候选地点' } }), 80)
        setTimeout(() => send('item_preview', { runId: 'offline', previewId: 'offline:1:2', dayNo: 1, itemOrdinal: 2, item: { itemType: 'attraction', poiName: '有效候选' } }), 250)
        setTimeout(() => send('item_preview_withdrawn', { runId: 'offline', previewId: 'offline:1:2', dayNo: 1, itemOrdinal: 2, reason: 'offline-withdrawal' }), 650)
        setTimeout(() => {
          detail = { ...detail, status: failure ? 3 : 2, genState: failure ? 'FAILED' : 'COMPLETED', coreReady: !failure,
            dayList: detail.dayList.map((day) => ({ ...day, generationStatus: failure ? 'FAILED' : 'SUCCEEDED',
              items: failure ? [] : [item(regression && day.dayNo === 1 ? '无关日期被改动' : `地点${day.dayNo}`), item(day.dayNo === 2 ? (scenario.requirements.requiredPlaces?.[0]?.name || '必去点') : '晚餐后散步', regression && day.dayNo === 1 ? '10:30' : '12:00', '13:00')] })) }
          if (!failure) send('core_ready', { daysEmitted: detail.days })
          send(failure ? 'error' : 'complete', failure ? { message: 'offline deliberate failure' } : {})
          controller.close()
        }, 1100)
      } }), { headers: { 'Content-Type': 'text/event-stream' } })
    }
    return response([])
  }
}

function offlineCheckpoints(scenario, final, regression) {
  const plans = (final?.dayList || []).map((day) => ({ day_no: day.dayNo, items: day.items.map((it) => ({
    item_type: it.itemType, poi_name: it.poiName, start_time: it.startTime, end_time: it.endTime, duration_min: it.durationMin, cost: it.cost,
  })) }))
  if (!plans.length || !plans[0].items.length) return { initial: null, day_checkpoints: [], missing: ['first_whole_trip_checkpoint'] }
  // Deliberately bad first draft: time overlap, plus a separate regression on an uninvolved day.
  plans.at(-1).items[1].start_time = '10:30'
  if (regression) { plans[0].items[0].poi_name = '地点1'; plans[0].items[1].start_time = '12:00' }
  return { baseline_definition: 'offline fixture; not a real checkpoint', initial: { plans }, day_checkpoints: [{ day_no: plans.length }] }
}

const browser = await pw.chromium.launch(channel === 'bundled' ? {} : { channel })
report.browser.version = browser.version()
report.browser.node_version = process.version
try {
  batch: for (let repeat = 1; repeat <= repeats; repeat++) for (const [index, scenario] of cases.entries()) {
    const beforeIdentity = JSON.parse(execFileSync(python, ['scripts/live_evidence.py'], { cwd: backend, encoding: 'utf8' }))
    assert.deepEqual(beforeIdentity, identity, 'Source/config/measurement changed: stop batch and retain prior attempts')
    const record = { scenario_id: scenario.id, repeat_id: repeat, started_at: new Date().toISOString(), payload: { days: scenario.slots.days }, errors: [] }
    report.runs.push(record)
    save() // An interrupted/failed run is still an attempted sample.
    const context = await browser.newContext({ locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: report.browser.viewport })
    try {
      const page = await context.newPage()
      const failure = !live && index === 2
      const regression = !live && index === 1
      if (!live) await page.addInitScript(installOfflineFixture, { scenario, failure, regression })
      await page.addInitScript(installPreviewTiming)
      page.on('pageerror', (error) => record.errors.push(String(error)))
      await page.route('**/*', (route) => {
        const url = new URL(route.request().url())
        if (url.origin !== new URL(base).origin) return route.abort()
        if (url.pathname === '/api/itinerary/clarify') return route.fulfill({ json: { code: 200, data: { slots: scenario.slots, state: { requirements: scenario.requirements }, ready: true, blocked: false, options: [], reply: '输入已冻结' } } })
        if (!live && url.pathname.startsWith('/api/')) return route.abort()
        return route.continue()
      })
      await page.goto(`${base}/`)
      if (live) {
        // Reuse the golden path's disposable account; no clarify model call.
        const account = { username: `evidence_${Date.now()}_${repeat}`, password: 'evidence-test-pass-123' }
        const registered = await context.request.post(`${base}/api/auth/register`, { data: account })
        assert.ok(registered.ok(), `register HTTP ${registered.status()}`)
        const loggedIn = await context.request.post(`${base}/api/auth/login`, { data: account })
        assert.ok(loggedIn.ok(), `login HTTP ${loggedIn.status()}`)
        await page.reload()
      }
      await page.locator('input[aria-label="说说你的旅行想法"]').fill(scenario.message)
      await page.getByRole('button', { name: '发送', exact: true }).click()
      await page.locator('.intake-start').click()
      try {
        await page.waitForFunction(() => window.__previewEvidence.details.some(({ detail }) => ['COMPLETED', 'PARTIAL', 'FAILED'].includes(detail.genState)), null, { timeout })
        await page.waitForFunction(() => window.__previewEvidence.core_visible_ms !== null ||
          ['PARTIAL', 'FAILED'].includes(window.__previewEvidence.details.at(-1)?.detail.genState), null, { timeout: 5000 })
      } catch (error) { record.errors.push(`terminal_wait:${String(error)}`) }
      const finalRead = await page.evaluate(async () => {
        const id = window.__previewEvidence.itinerary_id
        if (!id) return null
        const response = await fetch(`/api/itinerary/${id}`, { credentials: 'include' })
        if (!response.ok) throw new Error(`final DB read HTTP ${response.status}`)
        return (await response.json()).data
      })
      const captured = await page.evaluate(() => window.__previewEvidence)
      Object.assign(record, { payload: captured.payload || record.payload, itinerary_id: captured.itinerary_id,
        submitted_at: captured.submitted_at,
        browser: { first_candidate_ms: captured.first_candidate_ms, first_candidate_name: captured.first_candidate_name, core_visible_ms: captured.core_visible_ms },
        events: captured.events, detail_history: captured.details, final_detail: finalRead })
      record.errors.push(...captured.errors)
      for (const key of ['city', 'days', 'persons', 'budget']) {
        if (record.payload[key] !== scenario.slots[key]) record.errors.push(`input_mismatch:${key}`)
      }
      if (JSON.stringify(record.payload.requirementsStruct) !== JSON.stringify(scenario.requirements)) record.errors.push('input_mismatch:requirementsStruct')
      if (!live) record.checkpoints = offlineCheckpoints(scenario, record.final_detail, regression)
    } catch (error) { record.errors.push(String(error)) }
    finally {
      record.identity_end = JSON.parse(execFileSync(python, ['scripts/live_evidence.py'], { cwd: backend, encoding: 'utf8' }))
      if (JSON.stringify(record.identity_end) !== JSON.stringify(identity)) record.errors.push('identity_changed_during_run')
      record.finished_at = new Date().toISOString()
      save(); await context.close()
    }
    if (live && !['COMPLETED', 'PARTIAL', 'FAILED'].includes(record.final_detail?.genState)) {
      report.stop_reason = 'Terminal state unconfirmed: retain attempt and stop; browser timeout does not cancel server work.'
      save()
      break batch
    }
  }
} finally { await browser.close(); save() }
console.log(`Saved ${report.runs.length} attempts (${report.mode}): ${out}`)
if (!live) {
  assert.equal(report.runs.length, cases.length * repeats)
  assert.ok(report.runs.every((r) => r.browser?.first_candidate_name === '有效候选'), 'only named visible candidates count')
  assert.ok(report.runs[0].browser.core_visible_ms > report.runs[0].browser.first_candidate_ms)
  assert.equal(report.runs[2].final_detail.genState, 'FAILED')
  assert.equal(report.runs[2].browser.core_visible_ms, null)
  assert.ok(report.runs.every((r) => r.events.some((e) => e.frame.type === 'item_preview_withdrawn')))
}
