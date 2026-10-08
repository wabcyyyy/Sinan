import { useEffect, useMemo, useRef, useState } from 'react'
import { isOfflineError, isUnauthorized, listItineraries } from '../../api/sinan'
import { loginRedirect, navigate } from '../router'
import { destinations, sampleTripSummaries } from '../data'
import type { ItinerarySummary } from '../../types/itinerary'
import { Icon } from '../shared/Icon'
import { SmartImg } from '../shared/SmartImg'
import { PageHeader } from '../shared/PageHeader'
import { EmptyBlock, ErrorBlock, OfflineBadge } from '../shared/States'
import { deleteIntakeSession, formatSessionTime, listIntakeSessions } from '../home/intakeHistory'
import { saveIntake } from '../home/intakeSlots'
import type { IntakeSessionRecord } from '../home/intakeHistory'

type Filter = 'all' | 'active' | 'done' | 'draft'

export function TripsPage() {
  const [items, setItems] = useState<ItinerarySummary[]>([])
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [offline, setOffline] = useState(false)
  const [authExpired, setAuthExpired] = useState(false)
  const [drafts, setDrafts] = useState<IntakeSessionRecord[]>([])
  // FEUX-1：tab 快速切换时慢响应后到覆盖新响应（实测 B3 复现）——请求序号守卫，
  // 只有「最新一次 load」的结果才允许落地
  const loadSeqRef = useRef(0)

  const loadDrafts = () => {
    setDrafts(listIntakeSessions())
  }

  const load = () => {
    const seq = ++loadSeqRef.current
    const isStale = () => seq !== loadSeqRef.current
    setLoading(true)
    setError('')
    setOffline(false)
    setAuthExpired(false)
    loadDrafts()

    if (!localStorage.getItem('sinan-username')) {
      setItems(sampleTripSummaries)
      setOffline(true)
      setLoading(false)
      return
    }

    listItineraries(filter === 'draft' ? 'active' : filter, query)
      .then((next) => { if (!isStale()) setItems(next) })
      .catch((err: unknown) => {
        if (isStale()) return
        if (isUnauthorized(err)) {
          setItems([])
          setAuthExpired(true)
          setError('登录已失效，请重新登录后继续查看你的行程')
        } else if (isOfflineError(err)) {
          setItems(sampleTripSummaries)
          setOffline(true)
        } else {
          setError(err instanceof Error ? err.message : '行程加载失败')
        }
      })
      .finally(() => { if (!isStale()) setLoading(false) })
  }

  useEffect(() => {
    load()
  }, [filter])

  const visible = useMemo(
    () => items.filter((item) => !query || `${item.title}${item.city}`.includes(query)),
    [items, query],
  )

  const visibleDrafts = useMemo(
    () => drafts.filter((d) => !query || `${d.title}${d.slots.city || ''}`.includes(query)),
    [drafts, query],
  )

  const handleResumeDraft = (draft: IntakeSessionRecord) => {
    // M2：走 saveIntake 统一写入带版本的会话记录——手写 JSON 会缺版本号，
    // 首页 loadIntake 按版本校验会拒收，恢复条就白点了
    saveIntake({ messages: draft.messages, slots: draft.slots, firstMessage: draft.firstMessage })
    navigate('/')
  }

  const handleDeleteDraft = (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    deleteIntakeSession(id)
    loadDrafts()
  }

  return (
    <div className="page-content trips-page">
      <PageHeader
        eyebrow="My trips"
        title="我的行程"
        description="保存过的方向，会在这里继续展开。离线时可以查看最近的本地预览。"
        action={
          <button className="button button-primary" type="button" onClick={() => navigate('/')}>
            新建行程
            <Icon name="arrow" size={17} />
          </button>
        }
      />

      <div className="trips-toolbar">
        <div className="trip-tabs">
          {[
            ['all', '全部'],
            ['active', '进行中'],
            ['done', '已完成'],
            ['draft', `草稿 (${drafts.length})`],
          ].map(([key, label]) => (
            <button
              key={key}
              className={filter === key ? 'trip-tab active' : 'trip-tab'}
              type="button"
              onClick={() => setFilter(key as Filter)}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="search-field compact">
          <Icon name="search" size={17} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') load()
            }}
            placeholder="搜索行程或目的地"
            aria-label="搜索行程"
          />
        </label>
      </div>

      {offline && (
        <div className="offline-banner">
          <OfflineBadge />
          <span>当前无法连接行程服务，展示的是本地示例。登录并联网后会自动读取真实数据。</span>
        </div>
      )}

      {filter === 'draft' ? (
        visibleDrafts.length === 0 ? (
          <EmptyBlock
            title="暂无待完成的规划草稿"
            description="在首页对司南提出你的旅行想法，若中途退出，草稿会自动存放在这里随时继续。"
            action={{ label: '去开启新对话', onClick: () => navigate('/') }}
          />
        ) : (
          <div className="trip-grid">
            {visibleDrafts.map((draft) => (
              <article className="trip-card trip-card-draft" key={draft.id}>
                <div className="trip-card-cover" onClick={() => handleResumeDraft(draft)}>
                  <SmartImg
                    src={destinations.find((item) => item.city === draft.slots.city)?.image || destinations[0].image}
                    alt={`${draft.slots.city || '草稿'}参考风景`}
                    ratio="3 / 2"
                  />
                  <span className="trip-card-arrow">
                    <Icon name="arrow" size={16} />
                  </span>
                  <span className="trip-card-badge-draft">待继续对话</span>
                </div>
                <div className="trip-card-body">
                  <div className="card-kicker">
                    <span>{draft.slots.city || '未定城市'}</span>
                    <span>
                      {draft.slots.days ? `${draft.slots.days} 天` : '天数待定'} ·{' '}
                      {draft.slots.persons ? `${draft.slots.persons} 人` : '人数待定'}
                    </span>
                  </div>
                  <h2>{draft.title}</h2>
                  <p>{draft.firstMessage ? `初始意图：“${draft.firstMessage}”` : '中途保存的对话规划草稿'}</p>
                  <div className="trip-card-meta">
                    <span>更新于 {formatSessionTime(draft.updatedAt)}</span>
                    <div className="draft-card-actions">
                      <button
                        type="button"
                        className="text-action draft-del-btn"
                        onClick={(e) => handleDeleteDraft(draft.id, e)}
                      >
                        删除
                      </button>
                      <button
                        type="button"
                        className="button button-primary draft-resume-btn"
                        onClick={() => handleResumeDraft(draft)}
                      >
                        继续对话
                      </button>
                    </div>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )
      ) : loading ? (
        <div className="trip-skeleton-grid">
          <div />
          <div />
          <div />
        </div>
      ) : error ? (
        <ErrorBlock
          message={error}
          onRetry={load}
          onLogin={authExpired ? () => navigate(loginRedirect()) : undefined}
        />
      ) : visible.length === 0 ? (
        <EmptyBlock
          title="还没有保存的行程"
          description="从一句话开始，司南会帮你铺开第一段旅程。先看看别人怎么玩，或者直接说你的想法。"
          action={{ label: '开始规划', onClick: () => navigate('/') }}
          photos={destinations.slice(0, 4).map((item) => item.image)}
        />
      ) : (
        <div className="trip-grid">
          {visible.map((trip) => (
            <article className="trip-card" key={trip.id}>
              <a
                className="trip-card-cover"
                href={`/trips/${trip.id}`}
                onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return
                  event.preventDefault()
                  navigate(`/trips/${trip.id}`)
                }}
              >
                <SmartImg
                  src={trip.coverUrl || destinations.find((item) => item.city === trip.city)?.image || destinations[0].image}
                  alt={`${trip.city}城市风景`}
                  ratio="3 / 2"
                />
                <span className="trip-card-arrow">
                  <Icon name="arrowUpRight" size={17} />
                </span>
              </a>
              <div className="trip-card-body">
                <div className="card-kicker">
                  <span>{trip.city}</span>
                  <span>
                    {trip.days} 天 · {trip.persons} 人
                  </span>
                </div>
                <h2>{trip.title}</h2>
                <p>
                  {trip.tripTheme ||
                    (trip.id === 0 ? '这是一个可继续编辑的本地预览' : '一段等待出发的旅程')}
                </p>
                <div className="trip-card-meta">
                  <span className="trip-status-col">
                    {trip.status === 1 ? (
                      <em className="trip-generating">
                        <i />生成中
                      </em>
                    ) : trip.favorite ? (
                      <strong className="trip-fav-badge">
                        <Icon name="heart" size={12} />
                        已收藏
                      </strong>
                    ) : (
                      '最近打开'
                    )}
                  </span>
                  <span>{new Date(trip.createdAt).toLocaleDateString('zh-CN')}</span>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
