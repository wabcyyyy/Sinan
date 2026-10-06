import { useLayoutEffect, useRef } from 'react'

/** FLIP 回放时长（ms）：右栏淡入延迟（=时长 × 60%）经 .home-studio 上的内联变量
 * --flip-duration 消费同一个值（HomeStudio 注入），单一来源在本常量。 */
export const FLIP_DURATION_MS = 420

const EASE = 'cubic-bezier(.23, 1, .32, 1)' // 与 --sinan-ease 同曲线
const MOVE_PX = 4 // 位移阈值：小于它视为没换位（含 StrictMode 双挂载的重复测量）

/** FLIP（First–Last–Invert–Play）：对话卡布局切换前后各量一次 rect，用 WAAPI
 * element.animate() 从旧位置回放到新位置。零依赖；prefers-reduced-motion: reduce
 * 直接跳过（沿用 home-arrive 的取舍）。
 * StrictMode 双挂载幂等：ref 只缓存最近一次提交的几何，双挂载两次量到同一 rect、
 * 位移小于阈值不回放（教训同 useHomePlanning 的 resumedRef，PLAN 2026-10-02 §7.5）。
 * 只回放平移 + 横向缩放：idle→active 高差太大，纵向一起 invert 会把整段文字拉糊。 */
export function useFlip<T extends HTMLElement>(playing: boolean) {
  const ref = useRef<T | null>(null)
  const prev = useRef<{ x: number; y: number; w: number } | null>(null)
  const running = useRef<Animation | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const last = prev.current
    prev.current = { x: rect.left, y: rect.top, w: rect.width }
    if (!playing || !last) return
    const dx = last.x - rect.left
    const dy = last.y - rect.top
    const sx = last.w / rect.width
    if (Math.abs(dx) < MOVE_PX && Math.abs(dy) < MOVE_PX && Math.abs(sx - 1) < 0.02) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    running.current?.cancel()
    running.current = el.animate(
      [
        { transformOrigin: 'left top', transform: `translate(${dx}px, ${dy}px) scale(${sx}, 1)` },
        { transformOrigin: 'left top', transform: 'none' },
      ],
      { duration: FLIP_DURATION_MS, easing: EASE },
    )
  })
  return ref
}
