import type { ReactNode } from 'react'
import { navigate } from '../router'
import { TopNav } from '../navigation/TopNav'
import { BrandMark } from '../shared/Brand'

export function AppShell({ children, onLogin, onLogout, username, hideNav = false }: { children?: ReactNode; onLogin?: () => void; onLogout?: () => void; username?: string; hideNav?: boolean }) {
  return <div className="sinan-app"><a className="skip-link" href="#main-content">跳到主要内容</a>{!hideNav && <TopNav onLogin={onLogin} onLogout={onLogout} username={username} />}<main className="page-main" id="main-content">{children}</main><footer className="site-footer"><div className="footer-inner"><div><button className="footer-brand" type="button" onClick={() => navigate('/')}><BrandMark /><span><strong>司南 Sinan</strong><small>为每一段旅程找到方向</small></span></button></div><div className="footer-links"><button type="button" onClick={() => navigate('/')}>首页</button><button type="button" onClick={() => navigate('/explore')}>探索</button><button type="button" onClick={() => navigate('/trips')}>我的行程</button></div><span className="footer-note">行程事实可能变化，请在出发前再次核实。</span></div></footer></div>
}
