import { useEffect, useReducer, useState } from 'react'
import { TripBoard } from '../home/TripBoard'
import { DraftCard } from '../itinerary/ChatPanel'
import { TripMapPanel } from '../itinerary/TripMapPanel'
import { amapLink, buildPins } from '../itinerary/mapPins'
import { editMessage, initialState, phases, project, replayReducer } from './replay'
import './demo.css'

export function DemoPage() {
  const [state, dispatch] = useReducer(replayReducer, initialState)
  const [activeKey, setActiveKey] = useState<string | null>(null)
  useEffect(() => {
    if (!state.running) return
    const timer = setTimeout(() => dispatch('next'), 1200)
    return () => clearTimeout(timer)
  }, [state.running, state.step])
  const detail = project(state)
  const complete = state.step === phases.length - 1
  return <main className="demo-page sinan-app">
    <nav className="demo-nav"><a href="/">司南 Sinan</a><a href="https://github.com/wabcyyyy/Sinan">查看源码</a></nav>
    <header><p className="card-kicker">固定示例回放 · 无需登录 · 不消耗模型额度</p>
      <h1>在成都，把脚步放慢</h1>
      <p>两个人，两天一晚，预算 ￥1000。以下为合成固定示例，不是实时 AI 生成；费用、地点与开放时间均待核实。</p>
    </header>
    <section className="demo-controls" aria-label="回放控制">
      <button className="button button-primary" onClick={() => dispatch(state.running ? 'pause' : 'play')} disabled={complete}>{state.running ? '暂停' : '播放'}</button>
      <button className="button" onClick={() => dispatch('next')} disabled={complete}>单步</button>
      <button className="button" onClick={() => { dispatch('reset'); setActiveKey(null) }}>重置</button>
      <output aria-live="polite">{state.step + 1}/{phases.length} · {phases[state.step]}</output>
    </section>
    <ol className="demo-phases" aria-label="生成阶段">{phases.map((phase, index) => <li key={phase} aria-current={index === state.step ? 'step' : undefined} className={index <= state.step ? 'is-done' : ''}>{phase}</li>)}</ol>
    <div className="demo-grid">
      <section aria-label="示例行程">
        {detail.dayList.length ? <TripBoard key={state.step === 0 ? 'empty' : 'board'} draft={detail} onOpen={() => dispatch('draft')} openDisabled={!complete || state.applied} /> : <p className="demo-placeholder">点击播放或单步，查看行程逐日交付。</p>}
        {complete && <section className="demo-edit" aria-label="编辑确认示例">
          <h2>让第二天轻松一点</h2><p>示例编辑先展示变化，确认后才修改；第一天与住宿保持不变。</p>
          {state.showDraft && <><p>{editMessage.content}</p><DraftCard msg={editMessage} itineraryId={0} dayList={detail.dayList} applying={false} onApply={() => dispatch('apply')} onApplyHotel={() => {}} /></>}
          {state.applied && <p role="status">已应用固定编辑示例：第二天移除杜甫草堂，预算更新为 ￥{detail.totalAmount}。</p>}
        </section>}
      </section>
      <aside aria-label="示例地图">{detail.dayList.length ? <TripMapPanel days={detail.dayList} activeKey={activeKey} onSelect={(pin) => setActiveKey(pin.key)} /> : <p className="demo-placeholder">行程交付后显示地图。</p>}
        <p>底图需要联网；底图不可用时，行程列表与下方地点深链仍可使用。示例坐标均为估算。</p>
        <ul className="demo-links">{buildPins(detail.dayList).map((pin) => <li key={pin.key}><a href={amapLink(pin)} target="_blank" rel="noopener noreferrer">第 {pin.dayNo} 天 · {pin.poiName} · 地图核实</a></li>)}</ul>
      </aside>
    </div>
    <footer>固定回放仅演示交付与确认交互，不保存、分享或导出真实行程。真实生成请按 README 配置模型。</footer>
  </main>
}
