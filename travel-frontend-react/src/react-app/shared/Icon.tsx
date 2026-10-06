export type IconName =
  | 'arrow'
  | 'arrowUpRight'
  | 'search'
  | 'spark'
  | 'pin'
  | 'calendar'
  | 'users'
  | 'clock'
  | 'leaf'
  | 'sun'
  | 'moon'
  | 'chevron'
  | 'menu'
  | 'close'
  | 'heart'
  | 'share'
  | 'download'
  | 'refresh'
  | 'edit'
  | 'external'
  | 'check'
  | 'alert'
  | 'compass'
  | 'book'
  | 'grid'
  | 'mic'
  | 'image'
  | 'camera'
  | 'utensils'
  | 'bed'
  | 'train'
  | 'ticket'

interface IconProps {
  name: IconName
  size?: number
  strokeWidth?: number
  className?: string
}

export function Icon({ name, size = 20, strokeWidth = 1.8, className }: IconProps) {
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true, className }
  switch (name) {
    case 'arrow': return <svg {...common}><path d="M5 12h13" /><path d="m13 6 6 6-6 6" /></svg>
    case 'arrowUpRight': return <svg {...common}><path d="M5 19 19 5" /><path d="M9 5h10v10" /></svg>
    case 'search': return <svg {...common}><circle cx="10.8" cy="10.8" r="6.5" /><path d="m16 16 4.2 4.2" /></svg>
    case 'spark': return <svg {...common}><path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3Z" /><path d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" /></svg>
    case 'pin': return <svg {...common}><path d="M20 10.2c0 5.1-8 10.4-8 10.4S4 15.3 4 10.2a8 8 0 1 1 16 0Z" /><circle cx="12" cy="10" r="2.4" /></svg>
    case 'calendar': return <svg {...common}><rect x="3.5" y="5" width="17" height="15" rx="2" /><path d="M7.5 3.5v3M16.5 3.5v3M3.5 9.5h17" /></svg>
    case 'users': return <svg {...common}><circle cx="9" cy="9" r="3" /><path d="M3.5 19c.6-3.1 2.4-4.6 5.5-4.6s4.9 1.5 5.5 4.6M16 6.5a3 3 0 0 1 0 5.8M17 14.7c1.8.6 2.9 2 3.4 4.3" /></svg>
    case 'clock': return <svg {...common}><circle cx="12" cy="12" r="8.7" /><path d="M12 7v5l3 2" /></svg>
    case 'leaf': return <svg {...common}><path d="M20 4.5C12 4.2 6 7.3 6 13.2c0 3.8 2.8 6.3 6.2 6.3C18 19.5 20.2 12 20 4.5Z" /><path d="M4 20c3.5-4.5 7.5-6.4 12.5-8.3" /></svg>
    case 'sun': return <svg {...common}><circle cx="12" cy="12" r="3.2" /><path d="M12 2.7v2M12 19.3v2M4.1 4.1l1.4 1.4M18.5 18.5l1.4 1.4M2.7 12h2M19.3 12h2M4.1 19.9l1.4-1.4M18.5 5.5l1.4-1.4" /></svg>
    case 'moon': return <svg {...common}><path d="M20.6 13.2A8.4 8.4 0 0 1 10.8 3.4a8.4 8.4 0 1 0 9.8 9.8Z" /></svg>
    case 'chevron': return <svg {...common}><path d="m8 10 4 4 4-4" /></svg>
    case 'menu': return <svg {...common}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
    case 'close': return <svg {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>
    case 'heart': return <svg {...common}><path d="M20.8 8.8c0 5.2-8.8 10.3-8.8 10.3S3.2 14 3.2 8.8A4.8 4.8 0 0 1 12 6a4.8 4.8 0 0 1 8.8 2.8Z" /></svg>
    case 'share': return <svg {...common}><circle cx="18" cy="5" r="2.2" /><circle cx="6" cy="12" r="2.2" /><circle cx="18" cy="19" r="2.2" /><path d="m8 11 7.8-4.5M8 13l7.8 4.5" /></svg>
    case 'download': return <svg {...common}><path d="M12 4v11" /><path d="m7.5 11 4.5 4.5 4.5-4.5M5 20h14" /></svg>
    case 'refresh': return <svg {...common}><path d="M20 11a8 8 0 0 0-14.8-4L3 10" /><path d="M3 5v5h5M4 13a8 8 0 0 0 14.8 4L21 14" /><path d="M21 19v-5h-5" /></svg>
    case 'edit': return <svg {...common}><path d="m4 16.5-.8 3.3 3.3-.8L18 7.5 15.5 5 4 16.5Z" /><path d="m14.5 6 3.5 3.5" /></svg>
    case 'external': return <svg {...common}><path d="M14 5h5v5M19 5l-8 8" /><path d="M19 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h4" /></svg>
    case 'check': return <svg {...common}><path d="m5 12 4.5 4.5L19 7" /></svg>
    case 'alert': return <svg {...common}><path d="M12 4 21 20H3L12 4Z" /><path d="M12 9v5M12 17.3v.1" /></svg>
    case 'compass': return <svg {...common}><circle cx="12" cy="12" r="8.5" /><path d="m15.5 8.5-2.3 4.7-4.7 2.3 2.3-4.7 4.7-2.3Z" /></svg>
    case 'book': return <svg {...common}><path d="M5 4.5h11a3 3 0 0 1 3 3v12H8a3 3 0 0 0-3 0v-15Z" /><path d="M8 19.5V7.8a3.3 3.3 0 0 1 3-3.3" /></svg>
    case 'grid': return <svg {...common}><rect x="4" y="4" width="6" height="6" rx="1" /><rect x="14" y="4" width="6" height="6" rx="1" /><rect x="4" y="14" width="6" height="6" rx="1" /><rect x="14" y="14" width="6" height="6" rx="1" /></svg>
    case 'mic': return <svg {...common}><rect x="9" y="3.5" width="6" height="11" rx="3" /><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0" /><path d="M12 18v3" /><path d="M8.5 21h7" /></svg>
    case 'image': return <svg {...common}><rect x="3.5" y="5" width="17" height="14" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m5.5 17 4-4 3 3 3.5-3.5 3.5 3.5" /></svg>
    case 'camera': return <svg {...common}><path d="M4.5 8.5h3l1.7-2.5h5.6l1.7 2.5h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1Z" /><circle cx="12" cy="13.5" r="3.2" /></svg>
    case 'utensils': return <svg {...common}><path d="M5.5 3.5v6a2.5 2.5 0 0 0 5 0v-6" /><path d="M8 12v8.5" /><path d="M17.5 3.5c-1.7 1.2-2.5 3.4-2.5 6 0 2 .8 3 2 3.3V20.5" /><path d="M17.5 3.5V13" /></svg>
    case 'bed': return <svg {...common}><path d="M3.5 18.5v-11" /><path d="M3.5 15.5h17v3" /><path d="M3.5 12.5h17a0 0 0 0 1 0 0v3H3.5v-3Z" /><path d="M6.5 12.5v-2a1.5 1.5 0 0 1 1.5-1.5h3.5a1.5 1.5 0 0 1 1.5 1.5v2" /><path d="M14.5 12.5V11a1.5 1.5 0 0 1 1.5-1.5h2a1.5 1.5 0 0 1 1.5 1.5v1.5" /></svg>
    case 'train': return <svg {...common}><rect x="5.5" y="3.5" width="13" height="13.5" rx="2.5" /><path d="M5.5 10.5h13" /><path d="m7 21 2-3.5" /><path d="m17 21-2-3.5" /><path d="M9 14.2v.1M15 14.2v.1" /></svg>
    case 'ticket': return <svg {...common}><path d="M4 8.5a1.5 1.5 0 0 1 1.5-1.5h13A1.5 1.5 0 0 1 20 8.5v2a2 2 0 0 0 0 3v2a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 15.5v-2a2 2 0 0 0 0-3v-2Z" /><path d="M13.5 7.5v9" /></svg>
  }
}
