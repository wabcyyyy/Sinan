import { navigate, resetHomeSession } from '../router'

/**
 * 品牌标「S 路线」：司南 Sinan 的首字母 S，同时是一条弯向终点的路线——
 * 一条墨色 S 线，顶端一枚朱砂点标出「下一站」。纯几何（线+点），
 * 不描摹任何实物；favicon.svg 与 PWA 图标是同一几何的白描版（红底白线+镂空点）。
 */
export function BrandMark() {
  return (
    <span className="sinan-mark" aria-hidden="true">
      <svg viewBox="0 0 64 64" role="presentation" focusable="false">
        <g transform="rotate(6 32 32)">
          <path
            d="M41 18 C37 12.5 26 12.5 22.5 18.5 C19 24.5 25.5 28 32 32 C38.5 36 45 39.5 41.5 46 C38 52 27 52 23 46"
            fill="none"
            stroke="var(--sinan-ink)"
            strokeWidth="7.5"
            strokeLinecap="round"
          />
          <circle cx="47.5" cy="10.5" r="5" fill="var(--sinan-accent)" />
        </g>
      </svg>
    </span>
  )
}

/** 首页居中 Hero 品牌大标：渐变圆角徽章 + 白描 S 路线，具有极高辨识度与现代质感 */
export function HeroLogo() {
  return (
    <div className="hero-logo-emblem" aria-hidden="true">
      <svg viewBox="0 0 64 64" role="presentation" focusable="false" className="hero-logo-svg">
        <defs>
          <linearGradient id="heroLogoGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="var(--sinan-accent)" />
            <stop offset="100%" stopColor="var(--sinan-accent-dark)" />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="20" fill="url(#heroLogoGrad)" />
        <g transform="rotate(6 32 32)">
          <path
            d="M41 18 C37 12.5 26 12.5 22.5 18.5 C19 24.5 25.5 28 32 32 C38.5 36 45 39.5 41.5 46 C38 52 27 52 23 46"
            fill="none"
            stroke="var(--sinan-on-image)"
            strokeWidth="7"
            strokeLinecap="round"
          />
          <circle cx="47.5" cy="10.5" r="5.5" fill="var(--sinan-on-image)" />
        </g>
      </svg>
    </div>
  )
}

export function Brand({ compact = false }: { compact?: boolean }) {
  const handleClick = () => {
    resetHomeSession()
    navigate('/')
  }
  return <button className={compact ? 'sinan-brand compact' : 'sinan-brand'} type="button" onClick={handleClick} aria-label="司南 Sinan 首页">
    <BrandMark />
    <span className="sinan-brand-copy"><strong>司南</strong><small>Sinan</small></span>
  </button>
}
