import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../../api/sinan'
import { CURATED_ITINERARIES } from '../../types/generated/contracts'
import { makeDemoTrip, makeTemplateTrip } from '../demo/fixtures'
import { TripMetrics } from '../itinerary/TripBadges'
import { navigate } from '../router'
import { CopyItineraryButton } from './CopyItineraryButton'
import { PublicItineraries } from './PublicItineraries'
import { PublishItineraryPanel } from './PublishItineraryPanel'

vi.mock('../../api/sinan', async (original) => ({ ...await original<typeof import('../../api/sinan')>(),
  listPublicItineraries: vi.fn(), copyPublicItinerary: vi.fn(), copySharedItinerary: vi.fn(), previewPublicItinerary: vi.fn(), publishPublicItinerary: vi.fn(), unpublishPublicItinerary: vi.fn(),
}))
vi.mock('../router', async (original) => ({ ...await original<typeof import('../router')>(), navigate: vi.fn() }))
;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
let host: HTMLDivElement
let root: Root
beforeEach(() => { vi.clearAllMocks(); window.history.replaceState({}, '', '/itineraries/-1'); host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host) })
afterEach(async () => { await act(async () => root.unmount()); host.remove(); window.history.replaceState({}, '', '/') })
const click = async (label: string) => { const button = [...host.querySelectorAll('button')].find((node) => node.textContent?.includes(label)); expect(button).toBeTruthy(); await act(async () => button!.click()) }

it('does not present an unpriced copied route as a free trip', () => {
  const trip = makeTemplateTrip(CURATED_ITINERARIES[0], 201)
  const markup = renderToStaticMarkup(createElement(TripMetrics, { trip, dateText: null, weatherDays: 2, hasWeather: false }))
  expect(markup).toContain('待估算')
  expect(markup).not.toContain('￥0')
})

it('loads all editorial references and keeps the source label visible', async () => {
  vi.mocked(api.listPublicItineraries).mockResolvedValue(CURATED_ITINERARIES)
  await act(async () => root.render(createElement(PublicItineraries)))
  expect(host.querySelectorAll('.public-route-card')).toHaveLength(7)
  for (const city of ['杭州', '成都', '北京', '大理', '首尔', '厦门', '大阪']) expect(host.textContent).toContain(city)
  expect(host.textContent).toContain('不代表已实地完成出行')
})

it('opens the expanded inspiration route from the library', async () => {
  vi.mocked(api.listPublicItineraries).mockResolvedValue(CURATED_ITINERARIES)
  await act(async () => root.render(createElement(PublicItineraries)))
  const card = [...host.querySelectorAll<HTMLButtonElement>('.public-route-card')].find((node) => node.textContent?.includes('去大理过一半日子'))!
  await act(async () => card.click())
  expect(navigate).toHaveBeenLastCalledWith('/itineraries/-4')
})

it('requires login before copying and preserves the public detail destination', async () => {
  await act(async () => root.render(createElement(CopyItineraryButton, { templateId: -1 })))
  await click('登录后复制微调')
  expect(api.copyPublicItinerary).not.toHaveBeenCalled()
  expect(navigate).toHaveBeenCalledWith('/login?redirect=%2Fitineraries%2F-1')
})

it('locks repeated clicks until one copy is created and opens its private detail', async () => {
  let finish!: (value: { itineraryId: number; title: string }) => void
  vi.mocked(api.copyPublicItinerary).mockReturnValue(new Promise((resolve) => { finish = resolve }))
  await act(async () => root.render(createElement(CopyItineraryButton, { templateId: -1, signedIn: true })))
  const button = host.querySelector('button')!
  await act(async () => { button.click(); button.click() })
  expect(api.copyPublicItinerary).toHaveBeenCalledTimes(1)
  await act(async () => finish({ itineraryId: 901, title: '副本' }))
  expect(navigate).toHaveBeenCalledWith('/trips/901')
})

it('keeps errors visible without pretending the copy succeeded', async () => {
  vi.mocked(api.copyPublicItinerary).mockRejectedValue(new Error('这份路线已下架'))
  await act(async () => root.render(createElement(CopyItineraryButton, { templateId: 7, signedIn: true })))
  await click('复制到我的行程')
  expect(host.querySelector('[role=alert]')?.textContent).toContain('已下架')
  expect(navigate).not.toHaveBeenCalled()
})

it('previews the public projection before explicit publication, then supports unpublish', async () => {
  const summary = CURATED_ITINERARIES[0].summary
  vi.mocked(api.previewPublicItinerary).mockResolvedValue({ itineraryId: 101, publishedAt: null, summary })
  vi.mocked(api.publishPublicItinerary).mockResolvedValue({ itineraryId: 101, publishedAt: '2026-10-18', summary })
  vi.mocked(api.unpublishPublicItinerary).mockResolvedValue(null)
  await act(async () => root.render(createElement(PublishItineraryPanel, { trip: makeDemoTrip() })))
  await click('预览并发布')
  expect(api.publishPublicItinerary).not.toHaveBeenCalled()
  expect(host.textContent).toContain('不会公开你的日期')
  await click('确认公开发布')
  expect(api.publishPublicItinerary).toHaveBeenCalledWith(101)
  await click('下架公开规划')
  expect(api.unpublishPublicItinerary).toHaveBeenCalledWith(101)
})
