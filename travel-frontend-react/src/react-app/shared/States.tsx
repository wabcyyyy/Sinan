import type { CSSProperties } from 'react'
import { Icon } from './Icon'

export function LoadingBlock({ label = '正在读取…' }: { label?: string }) {
  return <div className="state-block loading-block" role="status" aria-live="polite"><span className="loading-orbit" /><strong>{label}</strong><span className="state-hint">司南正在整理可用信息</span></div>
}

export function EmptyBlock({ title, description, action, photos }: { title: string; description: string; action?: { label: string; onClick: () => void }; photos?: string[] }) {
  return <div className="state-block empty-block">{photos?.length
    ? <div className="empty-photos" aria-hidden="true">{photos.slice(0, 4).map((src, index) => <img key={src} src={src} alt="" loading="lazy" style={{ '--i': index } as CSSProperties} />)}</div>
    : <span className="empty-compass"><Icon name="compass" size={24} /></span>}<strong>{title}</strong><span className="state-hint">{description}</span>{action && <button className="button button-secondary" type="button" onClick={action.onClick}>{action.label}<Icon name="arrow" size={16} /></button>}</div>
}

/** FEUX-4：错误标题按内容分类——网络态才说「没连上」，找不到/登录过期/业务失败各有其词，
 * 不再用同一句标题和正文自相矛盾。 */
function errorTitle(message: string): string {
  if (message.includes('登录已失效') || message.includes('重新登录')) return '登录已过期'
  if (message.includes('不存在') || message.includes('没有找到')) return '没有找到这条内容'
  const hasCjk = /[\u4e00-\u9fff]/.test(message)
  return hasCjk ? '这一步没走通' : '暂时没有连上司南'
}

export function ErrorBlock({ message, onRetry, onLogin }: { message: string; onRetry?: () => void; onLogin?: () => void }) {
  return <div className="state-block error-block" role="alert"><span className="error-icon"><Icon name="alert" size={22} /></span><strong>{errorTitle(message)}</strong><span className="state-hint">{message}</span><div className="state-actions">{onRetry && <button className="button button-secondary" type="button" onClick={onRetry}><Icon name="refresh" size={16} />再试一次</button>}{onLogin && <button className="button button-primary" type="button" onClick={onLogin}>重新登录<Icon name="arrow" size={16} /></button>}</div></div>
}

/** FEUX-3：未知路径不再静默渲染首页——明确 404 语义并给回首页出口。 */
export function NotFoundBlock({ onHome }: { onHome: () => void }) {
  return <div className="state-block empty-block"><span className="empty-compass"><Icon name="compass" size={24} /></span><strong>这一页不存在</strong><span className="state-hint">地址可能打错了，检查一下或从下面回首页</span><div className="state-actions"><button className="button button-secondary" type="button" onClick={onHome}>回首页<Icon name="arrow" size={16} /></button></div></div>
}

export function OfflineBadge() {
  return <span className="status-badge status-offline"><span />离线示例</span>
}

export function QualityNotice({ status = 'DRAFT', pending = 0 }: { status?: string; pending?: number }) {
  const copy = status === 'READY' ? '已完成校验' : status === 'READY_WITH_WARNINGS' ? '已完成，部分事实需确认' : '草案，出发前请核实'
  return <div className="quality-notice"><span className="quality-dot" /> <span>{copy}</span>{pending > 0 && <small>{pending} 项待核实</small>}</div>
}
