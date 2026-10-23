import { useEffect, useState } from 'react'
import { navigate, resetHomeSession, useLocation } from '../router'
import { createEditTour } from './transport'
import './demo.css'

export function DemoBanner() {
  const [expanded, setExpanded] = useState(() => {
    try { return sessionStorage.getItem('sinan-demo:intro-seen') !== '1' } catch { return true }
  })
  const dismiss = () => {
    setExpanded(false)
    try { sessionStorage.setItem('sinan-demo:intro-seen', '1') } catch { /* 当前页面仍可收起。 */ }
  }
  const { path, query } = useLocation()
  const entering = path === '/' && query.get('tour') === 'edit'
  useEffect(() => {
    if (!entering) return
    // 初次挂载时先让父级路由完成监听；StrictMode 清理取消未执行的导航。
    const frame = requestAnimationFrame(() => navigate(`/trips/${createEditTour()}?tour=edit`))
    return () => cancelAnimationFrame(frame)
  }, [entering])

  return <aside className={`demo-banner${expanded ? ' is-expanded' : ''}`} aria-label="产品演示说明">
    <div className="demo-banner-heading"><button type="button" className="demo-banner-toggle" aria-expanded={expanded} aria-controls="demo-intro" onClick={() => setExpanded(!expanded)}>产品交互演示 <span>{expanded ? '−' : '说明与体验入口 ＋'}</span></button>{expanded && <button className="text-action" onClick={dismiss}>知道了，开始体验</button>}</div>
    {expanded && <div id="demo-intro" className="demo-intro"><p>使用真实产品界面与固定样例，不调用 AI；修改仅保存在当前标签页。先体验一次调整，也可以自由浏览路线。</p><div className="demo-banner-actions">
      <button className="button button-primary" onClick={() => navigate(`/trips/${createEditTour()}?tour=edit`)}>90秒体验局部修改</button>
      <button className="button button-secondary" onClick={() => { resetHomeSession(); navigate('/?intent=两个人去成都两天一晚，预算1200元，想慢慢逛') }}>体验规划流程</button>
      <button className="button button-secondary" onClick={() => navigate('/trips/101')}>看完整行程</button>
      <a href="https://github.com/wabcyyyy/Sinan/blob/master/docs/public/product-case-20261021.md" target="_blank" rel="noopener noreferrer">真实运行案例</a>
      <a href="https://github.com/wabcyyyy/Sinan" target="_blank" rel="noopener noreferrer">源码</a>
    </div></div>}
  </aside>
}
