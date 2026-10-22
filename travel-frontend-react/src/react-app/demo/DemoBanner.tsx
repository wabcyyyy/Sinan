import { useEffect, useRef } from 'react'
import { navigate, resetHomeSession, useLocation } from '../router'
import { resetDemo } from '../../demoMode'
import { createEditTour } from './transport'
import './demo.css'

export function DemoBanner() {
  const { path, query } = useLocation()
  const entering = path === '/' && query.get('tour') === 'edit'
  const started = useRef(false)
  useEffect(() => {
    if (!entering) { started.current = false; return }
    if (started.current) return
    started.current = true
    navigate(`/trips/${createEditTour()}?tour=edit`)
  }, [entering])

  return <aside className="demo-banner" aria-label="产品演示说明">
    <div><strong>产品交互演示</strong><span>真实页面 · 合成样例 · 不调用 AI · 仅保存在当前浏览器标签页</span></div>
    <div className="demo-banner-actions">
      <button className="button button-primary" onClick={() => navigate(`/trips/${createEditTour()}?tour=edit`)}>90秒体验局部修改</button>
      <button className="button button-secondary" onClick={() => { resetHomeSession(); navigate('/?intent=两个人去成都两天一晚，预算1200元，想慢慢逛') }}>体验规划流程</button>
      <button className="button button-secondary" onClick={() => navigate('/trips/101')}>看完整行程</button>
      <button className="text-action" onClick={resetDemo}>重置体验</button>
      <a href="https://github.com/wabcyyyy/Sinan/blob/master/docs/public/product-case-20261021.md" target="_blank" rel="noopener noreferrer">真实运行案例</a>
      <a href="https://github.com/wabcyyyy/Sinan" target="_blank" rel="noopener noreferrer">源码</a>
    </div>
  </aside>
}
