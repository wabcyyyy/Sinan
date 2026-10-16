// Installed before the app. Observe the real fetch/DOM path; never generate a second trip.
export function installPreviewTiming() {
  const record = window.__previewEvidence = {
    submitted_at: null, payload: null, itinerary_id: null, events: [], details: [], errors: [],
    first_candidate_ms: null, first_candidate_name: null, core_visible_ms: null,
  }
  let start = null
  let latest = null
  let queued = false
  const elapsed = () => start === null ? null : performance.now() - start
  const visible = (node) => {
    if (!node || document.visibilityState !== 'visible') return false
    const rect = node.getBoundingClientRect()
    const css = getComputedStyle(node)
    return css.visibility !== 'hidden' && css.display !== 'none' && Number(css.opacity) > 0 &&
      rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight &&
      rect.right > 0 && rect.left < innerWidth
  }
  const check = () => {
    queued = false
    if (start === null) return
    const candidate = [...document.querySelectorAll('.trip-preview-candidate-name, .day-item.is-preview h3')]
      .find((node) => visible(node) && node.textContent.trim() && node.textContent.trim() !== '候选地点')
    if (candidate && record.first_candidate_ms === null) {
      record.first_candidate_ms = elapsed()
      record.first_candidate_name = candidate.textContent.trim()
    }
    const days = latest?.dayList ?? []
    const authoritative = latest && (latest.coreReady || latest.genState === 'COMPLETED') &&
      days.length === record.payload?.days && days.every((d) => d.generationStatus === 'SUCCEEDED' && d.items?.length)
    const panelDays = [...document.querySelectorAll('.trip-preview-days > li.is-done')]
    const board = document.querySelector('.trip-board')
    const boardNames = [...document.querySelectorAll('.trip-board .day-item:not(.is-preview) h3')].map((n) => n.textContent.trim())
    const selectedDay = Number(document.querySelector('.board-day-chip.is-active strong')?.textContent.match(/\d+/)?.[0] || 1)
    const selected = days.find((d) => d.dayNo === selectedDay)
    const boardMatches = selected && boardNames.length === selected.items.length &&
      boardNames.every((name, i) => name === selected.items[i].poiName)
    const displayed = panelDays.length === record.payload?.days && panelDays.some(visible) &&
      panelDays.every((node, i) => days[i]?.items.slice(0, 4).every((item) => node.textContent.includes(item.poiName))) ||
      visible(board) && boardMatches && (record.payload?.days === 1 || document.querySelectorAll('.board-day-chip').length === record.payload?.days)
    if (authoritative && displayed && record.core_visible_ms === null) record.core_visible_ms = elapsed()
  }
  const schedule = () => {
    if (queued) return
    queued = true
    // Two animation frames: DOM observation after a rendering opportunity, not event receipt.
    requestAnimationFrame(() => requestAnimationFrame(check))
  }
  document.addEventListener('click', (event) => {
    if (event.target.closest?.('.intake-start') && start === null) {
      start = performance.now()
      record.submitted_at = new Date().toISOString()
    }
  }, true)
  new MutationObserver(schedule).observe(document, { childList: true, subtree: true, attributes: true })
  const original = window.fetch.bind(window)
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href)
    if (url.pathname === '/api/itinerary/generate' && init?.method === 'POST') {
      record.payload = JSON.parse(init.body)
    }
    if (start !== null && url.pathname === '/api/client-errors') record.errors.push({ error: 'frontend error probe fired', elapsed_ms: elapsed() })
    let response
    try { response = await original(input, init) } catch (error) {
      if (start !== null) record.errors.push({ path: url.pathname, error: String(error), elapsed_ms: elapsed() })
      throw error
    }
    if (start !== null && /^\/api\/itinerary\/(generate|\d+)$/.test(url.pathname)) {
      response.clone().json().then((envelope) => {
        const detail = envelope.data
        if (detail?.id) {
          if (record.itinerary_id !== null && record.itinerary_id !== detail.id) {
            record.errors.push({ error: 'itinerary identity changed' }); return
          }
          record.itinerary_id = detail.id
          latest = detail
          record.details.push({ received_ms: elapsed(), detail })
          schedule()
        } else record.errors.push({ path: url.pathname, status: response.status, envelope })
      }).catch((error) => record.errors.push({ error: String(error) }))
    }
    if (/\/api\/itinerary\/\d+\/events$/.test(url.pathname)) {
      const reader = response.clone().body?.getReader()
      if (reader) (async () => {
        const decoder = new TextDecoder()
        let buffer = ''
        while (true) {
          const chunk = await reader.read()
          if (chunk.done) break
          buffer += decoder.decode(chunk.value, { stream: true }).replaceAll('\r\n', '\n')
          let end
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2)
            const data = frame.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5)).join('\n')
            if (data) {
              try { record.events.push({ received_ms: elapsed(), frame: JSON.parse(data) }) }
              catch (error) { record.errors.push({ error: 'invalid SSE JSON', data }) }
            }
          }
        }
      })().catch((error) => record.errors.push({ error: String(error) }))
    }
    return response
  }
}
