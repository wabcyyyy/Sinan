import { useLocation } from '../router'
import { HomeStudio } from './HomeStudio'

/** 首页 = HomeStudio 工作台（PLAN 2026-10-02 §2）：idle 一张居中对话卡，一屏余量
 * 之后挂轻展示区 HomeShowcase（PLAN 2026-10-03 §2.2），开聊后变双栏。旧 home-story /
 * 重型灵感 / 攻略区块不回归，只留轻导流；query 变化（/?city= 等深链）时重挂会话，
 * 保 seedFromQuery 预填语义。 */
export function HomePage() {
  const { query } = useLocation()
  return <div className="home-page">
    <HomeStudio key={query.toString()} query={query} />
  </div>
}
