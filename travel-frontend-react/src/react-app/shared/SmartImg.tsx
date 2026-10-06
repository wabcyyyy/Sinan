import { useEffect, useRef, useState } from 'react'
import { Icon } from './Icon'

/**
 * 图片一等公民的统一出口：懒加载、等宽高比、shimmer 占位、加载淡入、失败兜底。
 * - 默认 loading=lazy + decoding=async；首屏大图传 eager（同时带上 fetchPriority）。
 * - ratio 传 CSS aspect-ratio 值（如 '4 / 3'），由外层盒子撑比例，图永远 object-fit: cover。
 *   传了 ratio 的场景不要再由父级给固定高度，二者会打架。
 * - 缓存命中时 React 可能错过 onLoad，挂载后主动查 complete 兜底。
 */
export function SmartImg({ src, alt, ratio, className = '', eager = false, width, height }: {
  src: string
  alt: string
  ratio?: string
  className?: string
  eager?: boolean
  width?: number
  height?: number
}) {
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading')
  const ref = useRef<HTMLImageElement>(null)

  useEffect(() => {
    if (ref.current?.complete) setState(ref.current.naturalWidth > 0 ? 'ok' : 'error')
  }, [])

  return (
    <span className={`smart-img ${className}`} style={ratio ? { aspectRatio: ratio } : undefined} data-state={state}>
      {state === 'error' ? (
        <span className="smart-img-fallback" role="img" aria-label={alt}><Icon name="image" size={20} /></span>
      ) : (
        <img
          ref={ref}
          src={src}
          alt={alt}
          width={width}
          height={height}
          loading={eager ? 'eager' : 'lazy'}
          decoding="async"
          fetchPriority={eager ? 'high' : undefined}
          onLoad={() => setState('ok')}
          onError={() => setState('error')}
        />
      )}
    </span>
  )
}
