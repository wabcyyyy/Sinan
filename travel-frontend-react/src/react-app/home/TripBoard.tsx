import { useState } from 'react'
import type { CSSProperties } from 'react'
import type { ItineraryDetail, TripItem } from '../../types/itinerary'
import { navigate } from '../router'
import { destinations } from '../data'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'
import { candidatesForDay } from './itemPreviews'
import type { ItemPreviewCandidate } from './itemPreviews'

/** 类型图标/徽标映射：与详情页时间线同款口径。详情页那份是 TripDetailPage.tsx 的
 * 模块内常量，这里刻意不 import——详情页链着 maplibre，预览板与它必须零模块依赖
 * （首页同步 import 链红线）。 */
const ITEM_TYPE_ICONS: Record<string, 'camera' | 'utensils' | 'bed' | 'train' | 'ticket'> = {
  attraction: 'camera',
  food: 'utensils',
  hotel: 'bed',
  transport: 'train',
  activity: 'ticket',
}

const ITEM_TYPE_LABELS: Record<string, string> = {
  attraction: '游览',
  food: '用餐',
  hotel: '住宿',
  transport: '交通',
  activity: '活动',
}

function displayDate(value: string | null | undefined) {
  if (!value) return ''
  const date = new Date(`${value}T00:00:00`)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}

/** done 态只读预览板（PLAN 2026-10-02 §3.5 + 拍板①）：生成完成后留在首页看预览，
 * 编辑与地图仍全部在详情页。数据全部沿用 useHomePlanning 的 draft（与详情页同源），
 * 不新发请求。类名契约：根 .trip-board、主 CTA .trip-board-cta 且 href=/trips/:id
 * （金路径 E2E 以此判定 done，卡6 接线）。时间线复用全局 itinerary.css 的 .day-item
 * 视觉语言（styles.css 全局引入，零 chunk 代价），只读态不渲染 item-meta/编辑控件，
 * 紧凑化覆盖写在 home.css 的 .trip-board 作用域内。头部天气行不做：draft
 * （ItineraryDetail）不带天气字段，详情页天气走独立的 /itinerary/:id/weather 请求，
 * 本板「不新发请求」红线下该行恒无数据可显。
 * M5a：previews 为当前在途候选（终态即清，常规 ready 进板时恒空）；若终态竞态下
 * 仍有迟到候选帧，按 dayNo 追加进对应天、带「正在完善」徽标，不静默丢弃。 */
export function TripBoard({ draft, previews, onReset }: {
  draft: ItineraryDetail
  previews?: ItemPreviewCandidate[]
  onReset?: () => void
}) {
  const dayList = draft.dayList
  // 点击切换当天；默认选中第一个已完成的天（逐日生成中途就绪的兜底：全空落第一天）
  const [pickedDay, setPickedDay] = useState<number | null>(null)
  const activeDay = dayList.find((day) => day.dayNo === pickedDay)
    ?? dayList.find((day) => day.items.length > 0)
    ?? dayList[0]
  const activeCandidates = activeDay ? candidatesForDay(previews ?? [], activeDay.dayNo) : []
  return <div className="trip-board" aria-label="行程预览">
    <header className="board-head">
      {draft.coverUrl && <figure className="board-cover"><SmartImg src={draft.coverUrl} alt={`${draft.city}行程封面`} ratio="4 / 1" /></figure>}
      <div className="board-headline">
        <span className="board-city">{draft.city}</span>
        <span className="board-meta">{draft.days} 天 · {draft.persons} 人</span>
        <span className="board-date"><Icon name="calendar" size={13} />{draft.startDate ? displayDate(draft.startDate) : '日期待定'}</span>
      </div>
    </header>
    {dayList.length > 1 && <div className="board-days" role="group" aria-label="按天切换">
      {dayList.map((day) => (
        <button
          key={day.dayNo}
          type="button"
          className={day.dayNo === activeDay?.dayNo ? 'board-day-chip is-active' : 'board-day-chip'}
          aria-current={day.dayNo === activeDay?.dayNo ? 'true' : undefined}
          onClick={() => setPickedDay(day.dayNo)}
        >
          <strong>DAY {String(day.dayNo).padStart(2, '0')}</strong>
          <small>{day.theme || (day.items.length ? `${day.items.length} 个安排` : '安排中…')}</small>
        </button>
      ))}
    </div>}
    {activeDay && <section className="board-day" aria-label={`第 ${activeDay.dayNo} 天安排`}>
      <h4 className="board-day-title">{activeDay.theme || `第 ${activeDay.dayNo} 天`}</h4>
      {activeDay.items.length || activeCandidates.length
        ? <div className="day-items" key={activeDay.dayNo}>
            {activeDay.items.map((item, index) => {
              const cityCover = draft.coverUrl || destinations.find((d) => d.city === draft.city)?.image || destinations[0].image
              return <BoardItem item={item} index={index} key={`${item.poiName}-${index}`} cityCover={cityCover} />
            })}
            {activeCandidates.map((candidate) => <BoardPreviewItem key={candidate.previewId} candidate={candidate} />)}
          </div>
        : <p className="board-day-empty">这一天还没有安排，到完整行程里点「重新生成这一天」即可补齐（不另扣今日 AI 次数）。</p>}
    </section>}
    <footer className="board-foot">
      {draft.budgetList.length > 0 && <div className="board-budget" aria-label="预算小计">
        <span className="board-budget-label">预算小计</span>
        <strong className="board-budget-total">￥{draft.totalAmount || draft.budget || 0}</strong>
        {draft.budgetList.map((row) => <em key={row.category}>{row.category} ￥{row.amount}</em>)}
      </div>}
      <p className="board-map-hint"><Icon name="pin" size={13} />地图与逐点编辑在完整行程里</p>
      <div className="board-actions">
        {onReset && (
          <button type="button" className="text-action trip-board-reset" onClick={onReset} title="返回首页重新开始">
            返回首页
          </button>
        )}
        <a
          className="button button-primary trip-board-cta"
          href={`/trips/${draft.id}`}
          onClick={(event) => {
            // 修饰键/非左键放行浏览器默认行为（新标签打开等），其余拦下走 SPA 路由
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return
            event.preventDefault()
            navigate(`/trips/${draft.id}`)
          }}
        >打开完整行程<Icon name="arrow" size={16} /></a>
      </div>
    </footer>
  </div>
}

/** 只读时间线条目：09:30–11:30 | 类型图标 | 名称 | 备注。类名与详情页 .day-item
 * 共用（视觉语言同源），无 item-meta / 行内编辑 / 证据控件——预览板只读。 */
function BoardItem({ item, index, cityCover }: { item: TripItem; index: number; cityCover?: string }) {
  const icon = ITEM_TYPE_ICONS[item.itemType] || 'pin'
  const remark = item.remark || item.whyThis
  const itemImg = item.image || item.imageUrl || cityCover
  return <article className="day-item" style={{ '--stagger-i': index } as CSSProperties}>
    <div className="day-item-time">{item.startTime || '--:--'}<span>{item.endTime || ''}</span></div>
    <div className="day-item-line"><i><Icon name={icon} size={11} strokeWidth={2.2} /></i><span /></div>
    <div className="day-item-copy">
      <div className="item-heading">
        <span className={`item-type item-type-${item.itemType}`}><Icon name={icon} size={12} strokeWidth={2} />{ITEM_TYPE_LABELS[item.itemType] || '安排'}</span>
        <h3>{item.poiName}</h3>
      </div>
      {remark && <p>{remark}</p>}
    </div>
    {itemImg && (
      <div className="day-item-media" aria-hidden="true">
        <SmartImg src={itemImg} alt={item.poiName || '地点缩略'} ratio="1 / 1" />
      </div>
    )}
  </article>
}

/** M5a 候选条目行：与正式条目同一 .day-item 视觉语言，候选态加「正在完善」徽标
 * （复用既有 .trip-preview-status-pill 徽标体系，不新造裸色）；候选未过 ground，
 * 不渲染媒体图与时间（坐标/关键事实未知），名称缺键走兜底文案。 */
function BoardPreviewItem({ candidate }: { candidate: ItemPreviewCandidate }) {
  const item = candidate.item
  const icon = ITEM_TYPE_ICONS[item.itemType ?? ''] || 'pin'
  return <article className="day-item is-preview" style={{ '--stagger-i': candidate.itemOrdinal } as CSSProperties}>
    <div className="day-item-time">--:--</div>
    <div className="day-item-line"><i><Icon name={icon} size={11} strokeWidth={2.2} /></i><span /></div>
    <div className="day-item-copy">
      <div className="item-heading">
        <span className={`item-type item-type-${item.itemType ?? 'unknown'}`}><Icon name={icon} size={12} strokeWidth={2} />{ITEM_TYPE_LABELS[item.itemType ?? ''] || '安排'}</span>
        <h3>{item.poiName || '候选地点'}</h3>
        <span className="trip-preview-status-pill is-active">{candidate.officialItemId ? '已并入行程' : '正在完善'}</span>
      </div>
      {(item.remark || item.whyThis) && <p>{item.remark || item.whyThis}</p>}
    </div>
  </article>
}
