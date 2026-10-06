#!/usr/bin/env node
/**
 * trek 双站像素对照（v2.8 视觉复刻验收，PLAN-前端视觉复刻-trek风格.md Phase 0）
 *
 * 对每一组路由：同视口（1440×900 @dpr1）截「本地 dev server」与「demo.liketrek.com」，
 * 产出三张图到 .tmp-vdiff/<name>/：local.png、trek.png、并排图 side-by-side.png、
 * pixelmatch 差异热图 diff.png（附差异率报告）。
 *
 * 口径：trek 是动态演示站（数据每小时重置、封面图随机），差异率只做**布局级**参考
 * （经验阈值 ≈8–12%），不追零像素；结构性的错位在热图上一眼可见。
 *
 * 用法：
 *   1) 先起本地前端：just dev-fe（默认 http://localhost:5173）
 *   2) node scripts/vdiff.mjs            # 跑全部用例
 *      node scripts/vdiff.mjs dashboard  # 只跑同名用例
 *
 * 依赖：playwright-core（复用本机 Chrome/Edge，不下载浏览器）、pixelmatch、pngjs、sharp（已有）。
 * 登录：两端都会尝试 demo 一键进入（trek 点「试用演示」；本地若被踢到 /login 会如实截图，
 * 操作者可按需先在带 GUI 的浏览器登录后改用 --keep-session 说明的方式扩展）。
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'
import pixelmatch from 'pixelmatch'
import { PNG } from 'pngjs'
import sharp from 'sharp'

const ROOT = dirname(dirname(fileURLToPath(import.meta.url))) // travel-frontend-react/
const OUT = join(ROOT, '.tmp-vdiff')
const VIEWPORT = { width: 1440, height: 900 }
const LOCAL_BASE = process.env.VDIFF_LOCAL ?? 'http://localhost:5173'
const TREK_BASE = 'https://demo.liketrek.com'
/** trek 数据动态：布局级告警线（差异率 > 此值在报告里标 WARN） */
const WARN_RATIO = 0.12

/** 路由用例：name 即输出目录名；trek 侧进地图行程页需先点 hero 卡（onTrek 可选钩子） */
const CASES = [
  { name: 'dashboard', localPath: '/', trekPath: '/dashboard' },
]

/** trek 有登录墙：/login?redirect=… 页点「试用演示」一键进 demo（2026-09-17 实测路径） */
async function trekDemoLogin(page) {
  if (!/\/login/.test(page.url())) return
  const demo = page.getByRole('button', { name: /试用演示|Try the demo/i })
  await demo.first().click()
  await page.waitForURL('**/dashboard', { timeout: 30_000 })
}

/** 找本机 Chrome/Edge（playwright-core 不带浏览器，复用已装的） */
function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  ].filter(Boolean)
  return candidates.find((p) => existsSync(p))
}

async function settle(page) {
  // 等 Vue 渲染与封面图解码：domcontentloaded 后再给网络一个喘息窗口（不用 networkidle，
  // 地图/图库长连接会永远挂起）
  await page.waitForLoadState('domcontentloaded')
  await page.waitForTimeout(3500)
}

async function shoot(page, url, file) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45_000 })
  await settle(page)
  await page.screenshot({ path: file })
}

async function main() {
  const only = process.argv[2]
  const cases = only ? CASES.filter((c) => c.name === only) : CASES
  if (!cases.length) throw new Error(`没有叫 ${only} 的用例`)

  const executablePath = findChrome()
  if (!executablePath) throw new Error('本机没找到 Chrome/Edge，设 CHROME_PATH 后重试')
  const browser = await chromium.launch({ executablePath, headless: true })
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1 })
  const page = await context.newPage()

  await mkdir(OUT, { recursive: true })
  const report = []

  for (const c of cases) {
    const dir = join(OUT, c.name)
    await mkdir(dir, { recursive: true })
    const localFile = join(dir, 'local.png')
    const trekFile = join(dir, 'trek.png')

    process.stdout.write(`[${c.name}] local … `)
    await shoot(page, `${LOCAL_BASE}${c.localPath}`, localFile)
    process.stdout.write('ok\ntrek … ')
    await shoot(page, `${TREK_BASE}${c.trekPath}`, trekFile)
    await trekDemoLogin(page)
    if (/\/login/.test(page.url()) === false && !page.url().endsWith(c.trekPath)) {
      await page.goto(`${TREK_BASE}${c.trekPath}`, { waitUntil: 'domcontentloaded' })
      await settle(page)
    }
    await page.screenshot({ path: trekFile })
    process.stdout.write('ok\n')

    const a = PNG.sync.read(await sharp(localFile).png().toBuffer())
    const b = PNG.sync.read(await sharp(trekFile).png().toBuffer())
    const w = Math.min(a.width, b.width)
    const h = Math.min(a.height, b.height)
    const crop = (img) =>
      sharp(img)
        .extract({ left: 0, top: 0, width: w, height: h })
        .png()
        .toBuffer()
        .then(PNG.sync.read)
    const diff = new PNG({ width: w, height: h })
    const mismatched = pixelmatch(crop(a).data, crop(b).data, diff.data, w, h, { threshold: 0.1 })
    const ratio = mismatched / (w * h)

    await writeFile(join(dir, 'diff.png'), PNG.sync.write(diff))
    await sharp({
      create: { width: w * 2 + 8, height: h, channels: 3, background: { r: 24, g: 24, b: 27 } },
    })
      .composite([
        { input: await sharp(localFile).png().toBuffer(), left: 0, top: 0 },
        { input: await sharp(trekFile).png().toBuffer(), left: w + 8, top: 0 },
      ])
      .png()
      .toFile(join(dir, 'side-by-side.png'))

    report.push({ name: c.name, ratio: (ratio * 100).toFixed(2) + '%', warn: ratio > WARN_RATIO })
  }

  await browser.close()
  console.log('\n==== vdiff 报告（差异率为布局级参考，trek 数据动态） ====')
  for (const r of report) {
    console.log(`${r.warn ? 'WARN' : ' ok '}  ${r.name.padEnd(12)} 差异 ${r.ratio}  → ${OUT}/${r.name}/`)
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
