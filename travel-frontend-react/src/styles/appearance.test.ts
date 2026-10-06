import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'

import {
  APPEARANCE_ATTRS,
  APPEARANCE_KEY,
  DEFAULT_APPEARANCE,
  applyAppearance,
  normalizeAppearance,
  readAppearance,
  saveAppearance,
  type AppearanceConfig,
} from './appearance'

describe('外观契约（appearance.ts）', () => {
  beforeEach(() => localStorage.clear())

  it('本地存储为空时回落默认', () => {
    expect(readAppearance()).toEqual(DEFAULT_APPEARANCE)
  })

  it('脏数据逐字段回落（坏 JSON / 未知 scheme / 非布尔 dark）', () => {
    localStorage.setItem(APPEARANCE_KEY, '{ 坏掉的 json')
    expect(readAppearance()).toEqual(DEFAULT_APPEARANCE)

    localStorage.setItem(
      APPEARANCE_KEY,
      JSON.stringify({ scheme: 'neon', density: 'tiny', dark: 'yes', reduceMotion: 1 }),
    )
    expect(readAppearance()).toEqual(DEFAULT_APPEARANCE)
  })

  it('合法值原样读出', () => {
    const config = { scheme: 'teal', dark: true, density: 'compact', reduceMotion: true }
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(config))
    expect(readAppearance()).toEqual(config)
  })

  it('applyAppearance 精确写 APPEARANCE_ATTRS 三个属性 + .dark 开关', () => {
    const target = document.createElement('div')
    applyAppearance(target, { scheme: 'contrast', dark: true, density: 'compact', reduceMotion: true })

    const written = [...target.attributes]
      .map((attr) => attr.name)
      .filter((name) => name.startsWith('data-'))
    expect(new Set(written)).toEqual(new Set(APPEARANCE_ATTRS))
    expect(target.getAttribute('data-scheme')).toBe('contrast')
    expect(target.getAttribute('data-density')).toBe('compact')
    expect(target.getAttribute('data-reduce-motion')).toBe('true')
    expect(target.classList.contains('dark')).toBe(true)

    applyAppearance(target, { ...DEFAULT_APPEARANCE })
    expect(target.getAttribute('data-scheme')).toBe('default')
    expect(target.getAttribute('data-reduce-motion')).toBe('false')
    expect(target.classList.contains('dark')).toBe(false)
  })

  it('normalizeAppearance 对 null / 非对象安全', () => {
    expect(normalizeAppearance(null)).toEqual(DEFAULT_APPEARANCE)
    expect(normalizeAppearance('teal')).toEqual(DEFAULT_APPEARANCE)
  })

  it('saveAppearance 落存储后可由 readAppearance 读回', () => {
    const config: AppearanceConfig = {
      scheme: 'contrast',
      dark: true,
      density: 'comfortable',
      reduceMotion: false,
    }
    saveAppearance(config)
    expect(readAppearance()).toEqual(config)
  })
})

describe('index.html 首帧引导脚本 = appearance.ts 的镜像（防两处漂移）', () => {
  // vitest 下 import.meta.url 不是 file: 协议，用 cwd（= travel-frontend-react）定位
  const html = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8')
  const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? ''

  it('引导脚本存在且引用同一 KEY', () => {
    expect(script).toContain(APPEARANCE_KEY)
  })

  it('脚本里 setAttribute 的属性名集合与 APPEARANCE_ATTRS 完全相等', () => {
    const names = new Set(
      [...script.matchAll(/setAttribute\('(data-[a-z-]+)'/g)].map((match) => match[1]),
    )
    expect(names).toEqual(new Set(APPEARANCE_ATTRS))
  })

  it('脚本内联在 <head>（首帧前执行，防 FOUC）且不依赖构建产物', () => {
    expect(html).toMatch(/<head>[\s\S]*<script>[\s\S]*setAttribute[\s\S]*<\/script>[\s\S]*<\/head>/)
    expect(script).not.toContain('import ')
  })
})
