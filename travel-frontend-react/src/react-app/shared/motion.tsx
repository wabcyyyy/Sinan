import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { animate, MotionConfig, useInView } from 'motion/react'
import type { Transition, Variants } from 'motion/react'

/** 全站动效口径（2026-10-12 动效改版）：「轻盈顺滑」——淡入上浮、依次出现、
 * 弹簧回弹只用在小元件（徽标/指示条）。时长与曲线对齐 theme.css 的
 * --lp-dur-* / --sinan-ease，CSS 动效与 JS 动效是同一种手感。 */

/** 与 --sinan-ease 同曲线 */
export const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1]

/** 指示条/选中态滑块：快、稳、几乎不过冲 */
export const SPRING_SNAPPY: Transition = { type: 'spring', stiffness: 520, damping: 40, mass: 0.8 }
/** 徽标/勾选落位：带一点点回弹的「啵」 */
export const SPRING_POP: Transition = { type: 'spring', stiffness: 460, damping: 22 }
/** 卡片/区块布局变化（高度增长、换位） */
export const SPRING_LAYOUT: Transition = { type: 'spring', stiffness: 320, damping: 34 }

/** 区块入场：淡入 + 轻微上浮 */
export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 12 },
  show: { opacity: 1, y: 0, transition: { duration: 0.42, ease: EASE_OUT } },
}

/** 容器：子项依次出现 */
export function staggerChildren(stagger = 0.06, delay = 0): Variants {
  return { hidden: {}, show: { transition: { staggerChildren: stagger, delayChildren: delay } } }
}

/** 小芯片「长出来」：缩放 + 淡入，弹簧落位 */
export const chipPop: Variants = {
  hidden: { opacity: 0, scale: 0.6, y: 6 },
  show: { opacity: 1, scale: 1, y: 0, transition: SPRING_POP },
}

/** 应用内「减少动效」开关（设置页写 html[data-reduce-motion]）与系统偏好合流：
 * 开关打开 → 一律 always；否则跟随系统（user）。MotionConfig 只认系统偏好，
 * 这里补上应用内开关，保证 JS 动效与 theme.css 的 CSS 压制同口径。 */
function useAppReducedMotion() {
  const read = () => typeof document !== 'undefined' && document.documentElement.getAttribute('data-reduce-motion') === 'true'
  const [reduced, setReduced] = useState(read)
  useEffect(() => {
    const html = document.documentElement
    const observer = new MutationObserver(() => setReduced(read()))
    observer.observe(html, { attributes: true, attributeFilter: ['data-reduce-motion'] })
    return () => observer.disconnect()
  }, [])
  return reduced
}

export function MotionRoot({ children }: { children: ReactNode }) {
  const reduced = useAppReducedMotion()
  return <MotionConfig reducedMotion={reduced ? 'always' : 'user'} transition={{ duration: 0.42, ease: EASE_OUT }}>{children}</MotionConfig>
}

function prefersReducedMotion() {
  if (typeof window === 'undefined') return true
  if (document.documentElement.getAttribute('data-reduce-motion') === 'true') return true
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
}

/** 数字滚动：首帧（含 SSR/静态渲染）直接是终值——文本永远正确，只在进入视口时
 * 从 0 补一段滚动；终值变化时从旧值滚到新值。减少动效下不滚。 */
export function CountUp({ value, prefix = '', suffix = '', duration = 0.9 }: { value: number; prefix?: string; suffix?: string; duration?: number }) {
  const ref = useRef<HTMLSpanElement | null>(null)
  const inView = useInView(ref, { once: true })
  const shown = useRef<number | null>(null)
  useEffect(() => {
    const el = ref.current
    if (!el || !inView) return
    const from = shown.current ?? 0
    shown.current = value
    if (from === value || prefersReducedMotion()) {
      el.textContent = `${prefix}${value}${suffix}`
      return
    }
    const controls = animate(from, value, {
      duration,
      ease: EASE_OUT,
      onUpdate: (latest) => { el.textContent = `${prefix}${Math.round(latest)}${suffix}` },
    })
    return () => controls.stop()
  }, [value, inView, prefix, suffix, duration])
  return <span ref={ref}>{`${prefix}${value}${suffix}`}</span>
}
