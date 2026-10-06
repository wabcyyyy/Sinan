import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// v2.6 §19.2 的令牌契约（机器可检）：字体换成 Geist 自托管。
// 与 appearance.test.ts 同一思路：读源码文本断言，防止令牌被误改/回退。
// （day-tint 令牌族已随最后的消费方删除并按 R5-8 先例移出断言，2026-10-04）
// （vitest 下 import.meta.url 不是 file: 协议，用 cwd = travel-frontend-react 定位）
const css = readFileSync(resolve(process.cwd(), 'src/styles/theme.css'), 'utf8')

describe('theme.css v2.6/v2.8 契约（字体）', () => {
  it('字体：Poppins 主字重自托管，Geist 保留为拉丁回退，Inter 已移除', () => {
    // v2.8 trek 视觉复刻：字体栈以 Poppins 打头（trek --font-system 实测首位），
    // Geist Sans 降为回退（trek --font-subtext 同款语义）；四个静态字重文件必须都在
    expect(css).toContain("font-family: 'Poppins'")
    for (const w of [400, 500, 600, 700]) {
      expect(css).toContain(`/fonts/poppins-latin-${w}.woff2`)
    }
    expect(css).toContain("font-family: 'Geist Sans'")
    expect(css).toContain('/fonts/geist-latin-var.woff2')
    expect(css).not.toContain("'Inter'")
    expect(css).toMatch(/--lp-font-ui:\s*'Poppins',\s*'Geist Sans'/)
  })
})
