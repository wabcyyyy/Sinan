import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { login, register, ReactApiError } from '../../api/sinan'
import { navigate, useLocation } from '../router'
import { Brand } from '../shared/Brand'
import hangzhou from '../../assets/img/cover-hangzhou.webp'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'

export function LoginPage({ onAuthenticated }: { onAuthenticated: (username: string) => void }) {
  const location = useLocation()
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { document.title = '登录 · 司南 Sinan'; return () => { document.title = '司南 Sinan' } }, [])
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError('')
    if (!username.trim() || password.length < 6) { setError('请输入账号和至少 6 位密码'); return }
    setBusy(true)
    try {
      if (mode === 'register') await register({ username: username.trim(), password, nickname: nickname.trim() || null })
      const result = await login({ username: username.trim(), password })
      onAuthenticated(result.user.username)
      const redirect = location.query.get('redirect')
      // // 开头会被浏览器当协议相对 URL 解析成异源地址，history.pushState 直接抛 SecurityError
      navigate(redirect && redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/trips')
    } catch (err) {
      setError(err instanceof ReactApiError ? err.message : '网络暂时不可用，请稍后再试')
    } finally { setBusy(false) }
  }
  return <div className="auth-page">
    <div className="auth-story">
      <SmartImg src={hangzhou} alt="杭州西湖的落日与远山" eager />
      <div className="auth-story-copy"><h2>把生活调慢，<br />把世界看近。</h2><p>从一个想法开始，去遇见下一段风景。</p><span>杭州 · 西湖</span></div>
    </div>
    <div className="auth-panel">
      <button className="auth-back" type="button" onClick={() => navigate('/')}><Icon name="arrow" size={16} />回到首页</button>
      <div className="auth-card">
        <Brand />
        <h1>{mode === 'login' ? '把你的旅程带走' : '创建你的司南账号'}</h1>
        <p>{mode === 'login' ? '登录后保存、编辑和分享行程。' : '注册后可以保存每一次出发的方向。'}</p>
        <form onSubmit={submit}>
          {mode === 'register' && <label>怎么称呼你<input value={nickname} onChange={(event) => setNickname(event.target.value)} autoComplete="nickname" placeholder="昵称（可选）" /></label>}
          <label>账号<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="用户名" /></label>
          <label>密码<input value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} type="password" placeholder="至少 6 位" /></label>
          {error && <div className="form-error" role="alert"><Icon name="alert" size={16} />{error}</div>}
          <button className="button button-primary auth-submit" type="submit" disabled={busy}>{busy ? '正在连接…' : mode === 'login' ? '登录司南' : '注册并登录'}<Icon name="arrow" size={17} /></button>
        </form>
        <button className="auth-switch" type="button" onClick={() => { setMode((value) => value === 'login' ? 'register' : 'login'); setError('') }}>{mode === 'login' ? '还没有账号？创建一个' : '已经有账号？直接登录'}</button>
        <p className="auth-footnote">你的行程由你掌握，司南只在你需要时保存它。</p>
      </div>
    </div>
  </div>
}
