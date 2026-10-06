import { navigate, useLocation } from '../router'
import { Icon } from '../shared/Icon'
import { PageHeader } from '../shared/PageHeader'
import { DestinationsTab } from './DestinationsTab'
import { GuidesTab } from './GuidesTab'
import { IdeaTab } from './IdeaTab'

/**
 * 探索页三合一（2026-10-02，PLAN §4）：目的地 / 旅行灵感 / 旅行攻略 收进一页，
 * `?tab=` 驱动（可直达、可刷新、可分享），默认 destinations；tab 切换写 query。
 * 三个 tab 主体 = 原三页内容原样迁移，只去掉了各自的 PageHeader。
 */
export type ExploreTab = 'destinations' | 'inspiration' | 'guides'

const tabs: { key: ExploreTab; label: string }[] = [
  { key: 'destinations', label: '目的地' },
  { key: 'inspiration', label: '旅行灵感' },
  { key: 'guides', label: '旅行攻略' },
]

/** 灵感主题全集（含「全部」）：IdeaTab 筛选 chips 与 readExploreTheme 校验共用这一份（2026-10-03 PLAN §2.5）。 */
export const exploreThemes = ['全部', '美食', '慢旅行', '亲子', '自然', '城市漫游']

/** 解析 ?tab=；缺省或非法值一律回默认 destinations。导出仅供测试。 */
export function readExploreTab(query: URLSearchParams): ExploreTab {
  const value = query.get('tab')
  return tabs.some((tab) => tab.key === value) ? (value as ExploreTab) : 'destinations'
}

/** 解析 ?theme=（灵感主题深链）；缺省或非法值一律回「全部」。导出仅供测试。 */
export function readExploreTheme(query: URLSearchParams): string {
  const value = query.get('theme')
  return value && exploreThemes.includes(value) ? value : '全部'
}

export function ExplorePage() {
  const location = useLocation()
  const tab = readExploreTab(location.query)
  return <div className="page-content explore-page">
    <PageHeader eyebrow="Explore" title="先找点方向" description="目的地、旅行灵感和攻略都收在这一页——找到方向，就把想法交给司南。" action={<button className="button button-primary" type="button" onClick={() => navigate('/')}>开始规划<Icon name="arrow" size={17} /></button>} />
    <div className="explore-tabs filter-tabs" role="tablist" aria-label="探索分区">{tabs.map((item) => <button key={item.key} role="tab" aria-selected={tab === item.key} className={tab === item.key ? 'filter-tab active' : 'filter-tab'} type="button" onClick={() => navigate(`/explore?tab=${item.key}`)}>{item.label}</button>)}</div>
    {tab === 'destinations' ? <DestinationsTab /> : tab === 'inspiration' ? <IdeaTab /> : <GuidesTab />}
  </div>
}
