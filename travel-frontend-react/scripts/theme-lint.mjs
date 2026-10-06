#!/usr/bin/env node
/**
 * theme-lint：外观契约的机器强制（SPEC v2.3 §7.1.6 / S0-11）。
 *
 * 扫描 `src/**` 的 .vue `<style>` 块与 .css 文件，三条禁令：
 *   ① 裸色值：禁止 #hex / rgb( / rgba( —— 颜色只能来自令牌变量。
 *      例外：**令牌定义块**（:root / [data-scheme=…] / .dark / prefers-color-scheme: dark）
 *      内部允许写具体色值（那正是令牌的唯一产地）。
 *   ② z-index 数字字面量：必须 var(--lp-z-*)，全站只有一把刻度。
 *   ③ !important：禁止（EP 适配改走 --el-* 变量，而不是堆优先级）。
 *      唯一例外：减少动效块（prefers-reduced-motion / [data-reduce-motion]）里的
 *      全局压制——a11y 语义必须能压过组件级动画，无法用变量表达。
 *
 * 【.ts 扩扫（R5-4）】JS 侧取色（canvas/内联 style/地图 paint）同样禁止裸 hex：
 *   只扫「产出视觉的 .ts」白名单（TS_VISUAL_FILES），且**仅色值规则**生效；
 *   `cssVar('--lp-*', …)` 行内作为离线兜底的 hex 视为令牌取值的合理回落，予以放行
 *   （与「令牌定义块」例外同理）。把新文件加进 TS_VISUAL_FILES 前先确认它可 0 裸 hex。
 *
 * 成功口径：`src/components/ui/**`、`src/styles/**`、`src/App.vue` 与本期新增 view 必须
 * 0 违规。未收敛的旧文件登记在 theme-lint.allowlist.json —— **只准变短不准变长**：
 *   - 新文件出现违规 = CI 红；已豁免文件中出现「比登记更多」的违规 = CI 红；
 *   - 修好一个文件就把它从列表删掉、把 _count 改小（只删不加）。
 *
 * 用法：
 *   node scripts/theme-lint.mjs                    # 检查（CI 用，非 0 退出即红）
 *   node scripts/theme-lint.mjs --update-allowlist # 维护者重算豁免表（慎用，会放宽既有违规）
 */

import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SRC = join(ROOT, 'src')
const ALLOWLIST_PATH = join(ROOT, 'scripts', 'theme-lint.allowlist.json')

// R5-4：产出视觉的 .ts（canvas 绘图/地图 paint/取色工具），纳入「仅色值」扫描。
const TS_VISUAL_FILES = [] // React 壳暂无「产出视觉的 .ts」；地图/取色工具入壳时在此登记（须可 0 裸 hex）

const TOKEN_BLOCK = /(:root\b|\[data-scheme|\.dark\b|prefers-color-scheme:\s*dark)/
const MOTION_BLOCK = /(prefers-reduced-motion|data-reduce-motion)/

const RULES = {
  color: {
    label: '裸色值',
    test: (line) => /(#[0-9a-fA-F]{3,8}\b|\brgba?\()/.test(line),
    skipInTokenBlock: true,
  },
  zindex: {
    label: 'z-index 字面量',
    test: (line) => /z-index\s*:\s*-?\d/.test(line),
    skipInTokenBlock: false,
  },
  important: {
    label: '!important',
    test: (line) => /!important\b/.test(line),
    skipInTokenBlock: false,
    skipInMotionBlock: true,
  },
}

function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) walk(path, acc)
    else if (name.endsWith('.css') || name.endsWith('.vue')) acc.push(path)
  }
  return acc
}

/** 注释等长置空（保留换行），避免注释里的示例色值误报、行号仍准确。 */
function blankComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
}

/** .vue 只保留 <style> 块内容参与扫描；其余区域等长置空，行号不变。 */
function maskVueToStyles(text) {
  let out = text.replace(/[^\n]/g, ' ')
  for (const m of text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
    const start = m.index + m[0].indexOf('>') + 1
    const end = start + m[1].length
    out = out.slice(0, start) + text.slice(start, end) + out.slice(end)
  }
  return out
}

function scanCss(text, file, out) {
  const lines = blankComments(text).split('\n')
  const stack = []
  lines.forEach((line, index) => {
    const openIndex = line.indexOf('{')
    const selector = openIndex >= 0 ? line.slice(0, openIndex).trim() : ''
    const context = openIndex >= 0 ? [...stack, selector] : stack
    const inTokenBlock = context.some((sel) => TOKEN_BLOCK.test(sel))
    const inMotionBlock = context.some((sel) => MOTION_BLOCK.test(sel))
    for (const [rule, def] of Object.entries(RULES)) {
      if (def.skipInTokenBlock && inTokenBlock) continue
      if (def.skipInMotionBlock && inMotionBlock) continue
      if (def.test(line)) {
        out.push({ file: relative(ROOT, file).split('\\').join('/'), line: index + 1, rule, text: line.trim().slice(0, 120) })
      }
    }
    for (const ch of line) {
      if (ch === '{') stack.push(selector || '?')
      else if (ch === '}') stack.pop()
    }
  })
}

/** .ts：`//` 行注释等长置空（目标文件内 `//` 不出现在字符串中，https:// 因前导 : 不匹配）。 */
function blankLineComments(text) {
  return text.replace(/(^|[^:])\/\/.*$/gm, '$1')
}

/** R5-4：扫「产出视觉的 .ts」的裸 hex —— 仅色值规则；cssVar('--lp-*', …) 行的兜底 hex 放行。 */
function scanTs(text, file, out) {
  const lines = blankLineComments(blankComments(text)).split('\n')
  lines.forEach((line, index) => {
    if (RULES.color.test(line) && !/cssVar\s*\(/.test(line)) {
      out.push({ file: relative(ROOT, file).split('\\').join('/'), line: index + 1, rule: 'color', text: line.trim().slice(0, 120) })
    }
  })
}

function collect() {
  const violations = []
  for (const file of walk(SRC)) {
    const raw = readFileSync(file, 'utf8')
    const text = file.endsWith('.vue') ? maskVueToStyles(raw) : raw
    scanCss(text, file, violations)
  }
  for (const rel of TS_VISUAL_FILES) {
    scanTs(readFileSync(join(ROOT, rel), 'utf8'), join(ROOT, rel), violations)
  }
  return violations
}

function groupByFile(violations) {
  const byFile = {}
  for (const v of violations) {
    byFile[v.file] = byFile[v.file] || []
    byFile[v.file].push(v)
  }
  return byFile
}

function main() {
  const update = process.argv.includes('--update-allowlist')
  const violations = collect()
  const byFile = groupByFile(violations)

  if (update) {
    const files = {}
    for (const file of Object.keys(byFile).sort()) files[file] = byFile[file].length
    const payload = {
      _note:
        'theme-lint 豁免列表（SPEC v2.3 §7.1.6）：只准变短不准变长——修好一个文件就从这里删掉并把 _count 改小；不许往里加新文件/加计数（新增违规请直接修代码）。F1 后 theme.css 与 App.vue 已清零移出；剩余项随 F2/F3 页面重做逐个删除。',
      _count: Object.keys(files).length,
      files,
    }
    writeFileSync(ALLOWLIST_PATH, JSON.stringify(payload, null, 2) + '\n')
    console.log(`[theme-lint] 已重算豁免表：${payload._count} 个文件、${violations.length} 处违规`)
    return 0
  }

  const allow = JSON.parse(readFileSync(ALLOWLIST_PATH, 'utf8'))
  const allowed = allow.files || {}
  const problems = []
  const stale = []

  for (const [file, list] of Object.entries(byFile)) {
    const recorded = allowed[file]
    if (recorded === undefined) {
      problems.push(`${file}: ${list.length} 处违规（不在豁免列表，必须直接修掉）`)
    } else if (list.length > recorded) {
      problems.push(`${file}: ${list.length} 处 > 豁免计数 ${recorded}（豁免只准变短）`)
    }
  }
  for (const [file, count] of Object.entries(allowed)) {
    if ((byFile[file] || []).length < count) stale.push(`${file}: 现 ${(byFile[file] || []).length} 处 < 登记 ${count} 处，可把计数改小`)
  }
  if (allow._count !== Object.keys(allowed).length) {
    problems.push(`_count=${allow._count} 与 files 数量 ${Object.keys(allowed).length} 不一致`)
  }

  const byRule = {}
  for (const v of violations) byRule[v.rule] = (byRule[v.rule] || 0) + 1
  const ruleText = Object.entries(byRule).map(([r, n]) => `${RULES[r].label} ${n}`).join(' / ') || '无'
  console.log(`[theme-lint] 违规 ${violations.length} 处（${ruleText}）；豁免文件 ${Object.keys(allowed).length} 个`)

  for (const item of stale) console.log(`  ~ ${item}`)
  if (problems.length) {
    console.error('[theme-lint] 未通过：')
    for (const p of problems) console.error('  - ' + p)
    console.error('  提示：新代码请只用 var(--lp-*) 令牌；确需例外先改代码而不是改豁免表。')
    return 1
  }
  console.log('[theme-lint] OK')
  return 0
}

process.exit(main())
