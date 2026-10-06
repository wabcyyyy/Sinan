#!/usr/bin/env node
/**
 * ep-allowlist：去 EP 的机器门禁（SPEC v2.6 §19.7-2）。
 *
 * 扫描 `src/**` 的 .vue/.ts（跳过生成的 *.d.ts 与测试文件），统计 Element Plus 的使用：
 *   ① 组件标签 `<el-…>`（含闭合标签）；② 服务式 API 标识符（ElMessage/ElMessageBox/…）。
 * 注释会被等长置空后再计数，避免注释里的提及污染计数。
 *
 * 成功口径：**总数只减不增**。
 *   - 出现不在允许清单里的文件 = 红（产品面禁止新增 EP 使用）；
 *   - 允许清单内文件计数**比登记更多** = 红；
 *   - 计数变少 = 提示（可把清单计数改小），不算红。
 * 允许清单 = scripts/ep-allowlist.json（含 admin/Generate/Login 等存量的冻结计数）。
 *
 * 用法：
 *   node scripts/ep-allowlist.mjs                  # 检查（CI 用）
 *   node scripts/ep-allowlist.mjs --update-allowlist  # 维护者重算（慎用：会放宽既有计数）
 */

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SRC = join(ROOT, 'src')
const ALLOWLIST_PATH = join(ROOT, 'scripts', 'ep-allowlist.json')

const SERVICE_API = /\b(ElMessageBox|ElMessage|ElNotification|ElLoading)\b/g
const EP_TAG = /<\/?el-[a-z][a-z-]*/g

function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) walk(path, acc)
    else if (
      (name.endsWith('.vue') || name.endsWith('.ts')) &&
      !name.endsWith('.d.ts') &&
      !name.endsWith('.test.ts')
    ) {
      acc.push(path)
    }
  }
  return acc
}

/** 注释等长置空（保留换行），注释里的提及不参与计数。 */
function blankComments(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:])\/\/[^\n]*/g, (m, p1) => p1 + ' '.repeat(m.length - p1.length))
}

function countEp(text) {
  const clean = blankComments(text)
  const tags = clean.match(EP_TAG) || []
  const apis = clean.match(SERVICE_API) || []
  return tags.length + apis.length
}

function collect() {
  const counts = {}
  for (const file of walk(SRC)) {
    const count = countEp(readFileSync(file, 'utf8'))
    if (count > 0) counts[relative(ROOT, file).split('\\').join('/')] = count
  }
  return counts
}

function main() {
  const update = process.argv.includes('--update-allowlist')
  const counts = collect()

  if (update) {
    const payload = {
      _note:
        'EP 允许清单（SPEC v2.6 §19.7-2）：只准变短不准变长——把 EP 从某个文件清干净就从这里删掉；不许加新文件/加计数。admin/Generate/Login 等存量面在清单内冻结。',
      _count: Object.keys(counts).length,
      files: Object.fromEntries(Object.keys(counts).sort().map((k) => [k, counts[k]])),
    }
    writeFileSync(ALLOWLIST_PATH, JSON.stringify(payload, null, 2) + '\n')
    const total = Object.values(counts).reduce((sum, n) => sum + n, 0)
    console.log(`[ep-allowlist] 已重算：${payload._count} 个文件、合计 ${total} 处 EP 使用`)
    return 0
  }

  const allow = JSON.parse(readFileSync(ALLOWLIST_PATH, 'utf8'))
  const allowed = allow.files || {}
  const problems = []
  const stale = []

  for (const [file, count] of Object.entries(counts)) {
    const recorded = allowed[file]
    if (recorded === undefined) {
      problems.push(`${file}: ${count} 处 EP 使用（不在允许清单，产品面禁止新增 EP）`)
    } else if (count > recorded) {
      problems.push(`${file}: ${count} 处 > 允许计数 ${recorded}（只准变短）`)
    }
  }
  for (const [file, count] of Object.entries(allowed)) {
    if ((counts[file] || 0) < count) stale.push(`${file}: 现 ${counts[file] || 0} 处 < 登记 ${count} 处，可把计数改小`)
  }
  if (allow._count !== Object.keys(allowed).length) {
    problems.push(`_count=${allow._count} 与 files 数量 ${Object.keys(allowed).length} 不一致`)
  }

  const total = Object.values(counts).reduce((sum, n) => sum + n, 0)
  console.log(`[ep-allowlist] EP 使用 ${total} 处，涉及 ${Object.keys(counts).length} 个文件（允许清单 ${Object.keys(allowed).length} 个）`)

  for (const item of stale) console.log(`  ~ ${item}`)
  if (problems.length) {
    console.error('[ep-allowlist] 未通过：')
    for (const p of problems) console.error('  - ' + p)
    console.error('  提示：产品面新代码请用 src/components/ui/ 的自研件；确需例外先改代码而不是改清单。')
    return 1
  }
  console.log('[ep-allowlist] OK')
  return 0
}

process.exit(main())
