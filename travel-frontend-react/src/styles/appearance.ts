/**
 * 外观契约：唯一运行时 DOM 写入口（SPEC v2.3 §7.1.4）。
 *
 * 约束（均有测试钉住）：
 * - 除本文件与 index.html 的首帧引导脚本外，任何代码不得写 documentElement 的
 *   样式/属性；index.html 的脚本是本模块的无依赖镜像，共用 APPEARANCE_KEY 与
 *   APPEARANCE_ATTRS 两个常量（测试读 index.html 文本比对，防两处漂移）；
 * - scheme 是**数据**：具体色值只存在 theme.css 的 [data-scheme] / .dark 块里；
 * - 本地存储损坏或出现未知值时逐字段回落默认，不让脏数据把界面卡死。
 */

export type SchemeId = 'default' | 'teal' | 'contrast' | 'indigo' | 'rose' | 'amber' | 'violet'
export type DensityId = 'comfortable' | 'compact'

export interface AppearanceConfig {
  scheme: SchemeId
  dark: boolean
  density: DensityId
  reduceMotion: boolean
}

export const APPEARANCE_KEY = 'ta-appearance-v1'
export const APPEARANCE_ATTRS = ['data-scheme', 'data-density', 'data-reduce-motion'] as const

export const DEFAULT_APPEARANCE: AppearanceConfig = {
  scheme: 'default',
  dark: false,
  density: 'comfortable',
  reduceMotion: false,
}

const SCHEME_IDS = new Set<unknown>([
  'default',
  'teal',
  'contrast',
  'indigo',
  'rose',
  'amber',
  'violet',
])
const DENSITY_IDS = new Set<unknown>(['comfortable', 'compact'])

export function normalizeAppearance(raw: unknown): AppearanceConfig {
  const source = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return {
    scheme: SCHEME_IDS.has(source.scheme)
      ? (source.scheme as SchemeId)
      : DEFAULT_APPEARANCE.scheme,
    density: DENSITY_IDS.has(source.density)
      ? (source.density as DensityId)
      : DEFAULT_APPEARANCE.density,
    dark: source.dark === true,
    reduceMotion: source.reduceMotion === true,
  }
}

export function readAppearance(): AppearanceConfig {
  try {
    const stored = localStorage.getItem(APPEARANCE_KEY)
    return stored ? normalizeAppearance(JSON.parse(stored)) : { ...DEFAULT_APPEARANCE }
  } catch {
    return { ...DEFAULT_APPEARANCE }
  }
}

export function applyAppearance(
  html: HTMLElement,
  config: AppearanceConfig = readAppearance(),
): void {
  html.setAttribute('data-scheme', config.scheme)
  html.setAttribute('data-density', config.density)
  html.setAttribute('data-reduce-motion', config.reduceMotion ? 'true' : 'false')
  html.classList.toggle('dark', config.dark)
}

/** 写入本地存储（隐私模式等失败时仅本次会话生效，不阻断交互）。 */
export function saveAppearance(config: AppearanceConfig): AppearanceConfig {
  try {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(config))
  } catch {
    /* 忽略 */
  }
  return config
}
