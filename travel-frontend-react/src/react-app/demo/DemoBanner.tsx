import { navigate, resetHomeSession } from '../router'
import { resetDemo } from '../../demoMode'
import './demo.css'

export function DemoBanner() {
  return <aside className="demo-banner" aria-label="产品演示说明">
    <div><strong>产品交互演示</strong><span>真实页面 · 合成样例 · 不调用 AI · 仅保存在当前浏览器标签页</span></div>
    <div className="demo-banner-actions">
      <button className="button button-secondary" onClick={() => { resetHomeSession(); navigate('/?intent=两个人去成都两天一晚，预算1200元，想慢慢逛') }}>体验规划流程</button>
      <button className="button button-secondary" onClick={() => navigate('/trips/101')}>看完整行程</button>
      <button className="text-action" onClick={resetDemo}>重置体验</button>
      <a href="https://github.com/wabcyyyy/Sinan" target="_blank" rel="noopener noreferrer">源码</a>
    </div>
  </aside>
}
