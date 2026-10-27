// 新建自制匿名输入页并截图；不修改原四张图片。
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import pw from 'playwright-core'

const root = resolve('../travel-agent-python/tests/fixtures/screenshots/night-boundaries')
mkdirSync(root, { recursive: true })
const cases = {
  ambiguous: '<h1>Weekend museum shortlist</h1><p>Louvre</p><p>Ticket: $25</p><p>A candidate only. Confirm the branch first.</p><p>City and date are not supplied.</p>',
  blurred: '<h1>Museum reservation</h1><p>Anonymous synthetic example</p><p class="unreadable">Museum QPZ-73 / Branch Y</p><p class="unreadable">2031-07-19 13:20 to 14:35</p><p class="unreadable">Pacific/Chatham — NZD 37.80</p><p>Please use a clearer copy for booking details.</p>',
}
const browser = await pw.chromium.launch({ channel: 'msedge', headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 900, height: 620 }, deviceScaleFactor: 1 })
  for (const [name, body] of Object.entries(cases)) {
    const html = '<!doctype html><meta charset="UTF-8"><style>body{margin:50px;font:26px/1.8 Arial;background:#fff;color:#202020}h1{font-size:34px}.unreadable{filter:blur(15px);user-select:none}</style>' + body
    writeFileSync(resolve(root, `${name}.html`), html)
    await page.setContent(html)
    await page.screenshot({ path: resolve(root, `${name}.png`) })
  }
} finally { await browser.close() }
console.log(root)
