import { act } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { confirmScreenshot, extractScreenshot, resolveScreenshot, registerManualScreenshotPlace } from '../../api/sinan'
import type { ScreenshotDocumentVO, ScreenshotResolveVO, TripRequirements } from '../../types/generated/contracts'
import { ScreenshotImport } from './ScreenshotImport'

vi.mock('../../api/sinan', async importOriginal => ({
  ...await importOriginal<typeof import('../../api/sinan')>(),
  confirmScreenshot: vi.fn(), extractScreenshot: vi.fn(), resolveScreenshot: vi.fn(), registerManualScreenshotPlace: vi.fn(),
}))

const req: TripRequirements = {
  factSource: null, travelPlanner: 'generate', travelConstraints: null,
  schemaVersion: 1, dayWindows: [], requiredPlaces: [], fixedArrangements: [], placeRelations: [], placeInputs: [],
  constraintPlanning: { countryCode: 'FR', timeZone: 'Europe/Paris', currency: 'EUR', activeEvidence: null }, excludedPlaces: [], excludedCategories: [],
  pace: null, maxAttractionsPerDay: null, dietaryRequirement: null, diningEvidencePolicy: null,
  transportPreference: null, maxWalkMinutesPerLeg: null, budgetPolicy: null, lodging: null, unresolvedRequests: [], requirementSources: [],
}
const doc: ScreenshotDocumentVO = {
  documentId: 'doc-1', imageId: 'image-1', width: 1000, height: 760, documentKind: 'guide',
  extractedAt: '2026-10-24T11:00:00Z', extractor: 'frozen-reference', questions: [], entries: [{
    recordId: '1', kind: 'place', polarity: 'recommended', questions: [], fields: [{ key: 'name', value: '卢浮宫',
      quote: '推荐 Musée du Louvre / 卢浮宫', language: 'fr/zh', region: { x: .1, y: .2, width: .7, height: .1 } }],
  }],
}
const resolved: ScreenshotResolveVO = { resolutionId: 'match-1', status: 'choices', question: '请确认具体馆区', choices: [
  { entityId: 'osm:relation:1', name: 'Musée du Louvre', aliases: ['卢浮宫'], address: 'Paris 75001', countryCode: 'FR',
    latitude: 48.86, longitude: 2.33, sourceUrl: 'https://www.openstreetmap.org/relation/1', retrievedAt: null, category: 'tourism', entityType: 'museum', identitySource: 'nominatim', matchScore: 70, matchReasons: ['跨语言别名一致'] },
  { entityId: 'osm:node:2', name: '同名另一馆区', aliases: ['Louvre'], address: '另一地址', countryCode: 'FR',
    latitude: 48.85, longitude: 2.34, sourceUrl: 'https://www.openstreetmap.org/node/2', retrievedAt: null, category: 'tourism', entityType: 'museum', identitySource: 'nominatim', matchScore: 0, matchReasons: ['具体馆区待确认'] },
] }

describe('screenshot input review', () => {
  let host: HTMLDivElement, root: Root
  const adopted = vi.fn()
  beforeEach(() => {
    vi.clearAllMocks()
    Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
    vi.stubGlobal('URL', class extends URL { static createObjectURL() { return 'blob:fixture' } static revokeObjectURL() {} })
    host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host)
    vi.mocked(extractScreenshot).mockResolvedValue(doc)
    vi.mocked(resolveScreenshot).mockResolvedValue(resolved)
    vi.mocked(confirmScreenshot).mockResolvedValue({ requirements: req, booking: null, notice: '待重新确认，正式行程未变更' })
  })
  afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals() })
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.includes(text))!
  const input = (label: string) => [...host.querySelectorAll('label')].find(l => l.textContent?.startsWith(label))!.querySelector<HTMLInputElement>('input')!
  const change = async (element: HTMLInputElement | HTMLSelectElement, value: string) => {
    await act(async () => {
      const prototype = element.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype
      Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value)
      element.dispatchEvent(new Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }))
    })
  }
  const render = async (bookingOnly = false) => { await act(async () => root.render(<ScreenshotImport requirements={req} city="Paris" days={1} startDate="2026-10-25" bookingOnly={bookingOnly} onAdopt={adopted} />)) }
  const upload = async () => {
    const fileInput = host.querySelector<HTMLInputElement>('input[type=file]')!
    Object.defineProperty(fileInput, 'files', { value: [new File(['fixture'], 'guide.png', { type: 'image/png' })], configurable: true })
    await act(async () => fileInput.dispatchEvent(new Event('change', { bubbles: true })))
    await act(async () => button('识别截图字段').click())
  }
  const chooseEntity = async () => {
    await act(async () => button('查询地点实体').click())
    expect(button('确认加入需求表单')?.disabled ?? button('确认并保存已订资料').disabled).toBe(true)
    await act(async () => host.querySelector<HTMLInputElement>('input[type=radio]')!.click())
  }

  it('requires explicit entity selection and preserves evidence before adopting a recommendation', async () => {
    await render(); await upload()
    expect(resolveScreenshot).not.toHaveBeenCalled(); expect(adopted).not.toHaveBeenCalled()
    await act(async () => button('name：').click())
    expect(host.querySelector('[aria-label="当前字段证据区域"]')?.getAttribute('style')).toContain('left: 10%')
    expect(host.textContent).toContain('推荐 Musée du Louvre / 卢浮宫')
    await chooseEntity()
    await act(async () => button('确认加入需求表单').click())
    expect(confirmScreenshot).toHaveBeenCalledWith(expect.objectContaining({ documentId: 'doc-1', recordId: '1',
      resolutionId: 'match-1', entityId: 'osm:relation:1', disposition: 'candidate', requirements: req }))
    expect(adopted).toHaveBeenCalledWith(req, null)
    expect(host.textContent).toContain('正式行程未变更')
  })

  it('无地图结果时登记待定位手工地点，经明确选择再采用', async () => {
    vi.mocked(resolveScreenshot).mockResolvedValue({ resolutionId: 'empty', status: 'not_found', question: '未返回实体', choices: [] })
    vi.mocked(registerManualScreenshotPlace).mockResolvedValue({ resolutionId: 'manual-match', status: 'choices', question: '请确认手工地点', choices: [{
      ...resolved.choices[0], entityId: 'manual:test', identitySource: 'user_provided', latitude: null, longitude: null, sourceUrl: null,
      matchScore: 0, matchReasons: ['坐标待定位'],
    }] })
    await render(); await upload()
    await act(async () => button('查询地点实体').click())
    await change(input('手工地址'), '用户核对的地址')
    await act(async () => button('登记手工地点').click())
    expect(registerManualScreenshotPlace).toHaveBeenCalledWith(expect.objectContaining({ address: '用户核对的地址', latitude: null, longitude: null }))
    expect(adopted).not.toHaveBeenCalled()
    expect(host.textContent).toContain('手工输入，未核验')
    await act(async () => host.querySelector<HTMLInputElement>('input[type=radio]')!.click())
    await act(async () => button('确认加入需求表单').click())
    expect(confirmScreenshot).toHaveBeenCalledWith(expect.objectContaining({ entityId: 'manual:test' }))
  })
  it('clears an old entity choice when the query name or branch changes', async () => {
    await render(); await upload(); await chooseEntity()
    await change(input('地点名称'), 'Louvre-Lens')
    expect(button('确认加入需求表单').disabled).toBe(true)
    expect(host.querySelector('input[type=radio]')).toBeNull()
    expect(confirmScreenshot).not.toHaveBeenCalled()
  })
  it('requires separate consent to override negative source semantics', async () => {
    vi.mocked(extractScreenshot).mockResolvedValue({ ...doc, entries: [{ ...doc.entries[0], polarity: 'excluded' }] })
    await render(); await upload(); await chooseEntity()
    const selection = [...host.querySelectorAll('label')].find(l => l.textContent?.startsWith('采用方式'))!.querySelector('select')!
    expect(selection.value).toBe('excluded')
    await change(selection, 'required')
    expect(button('确认加入需求表单').disabled).toBe(true)
    await act(async () => host.querySelector<HTMLInputElement>('input[type=checkbox]')!.click())
    await act(async () => button('确认加入需求表单').click())
    expect(confirmScreenshot).toHaveBeenCalledWith(expect.objectContaining({ disposition: 'required', overridePolarity: true }))
  })
  it('keeps missing dates and zone blank and saves explicit overnight booking through the existing callback', async () => {
    vi.mocked(extractScreenshot).mockResolvedValue({ ...doc, documentKind: 'booking', entries: [{ ...doc.entries[0], kind: 'transport' }] })
    await render(true); await upload(); await chooseEntity()
    expect(vi.mocked(extractScreenshot).mock.calls[0][1]).toBe('booking')
    expect(input('开始当地日期').value).toBe(''); expect(input('IANA').value).toBe('')
    await change(input('开始当地日期'), '2026-10-25'); await change(input('结束当地日期'), '2026-10-26')
    await change(input('开始当地时间'), '23:00'); await change(input('结束当地时间'), '06:00')
    await change(input('IANA'), 'Europe/Paris'); await change(input('原币金额'), '22'); await change(input('原币代码'), 'USD')
    await act(async () => button('确认并保存已订资料').click())
    expect(confirmScreenshot).toHaveBeenCalledWith(expect.objectContaining({ disposition: 'booking', localDate: '2026-10-25', endDate: '2026-10-26',
      startTime: '23:00', endTime: '06:00', timeZone: 'Europe/Paris', amount: '22', currency: 'USD' }))
  })
  it('shows unavailable geocoding without creating or accepting a guessed entity', async () => {
    vi.mocked(resolveScreenshot).mockResolvedValue({ resolutionId: 'match-2', status: 'unavailable', choices: [], question: '来源暂不可用，保留原字段' })
    await render(); await upload(); await act(async () => button('查询地点实体').click())
    expect(host.textContent).toContain('来源暂不可用')
    expect(button('确认加入需求表单').disabled).toBe(true)
    expect(confirmScreenshot).not.toHaveBeenCalled()
  })
  it('keeps the real booking end-date omission visible until the user supplies it', async () => {
    const recorded = JSON.parse(readFileSync(resolve(process.cwd(), '../travel-agent-python/tests/fixtures/screenshots/real_vision_20261024.json'), 'utf8').replace(/^\uFEFF/, '')) as { results: { fixture: string; extraction: Pick<ScreenshotDocumentVO, 'entries' | 'questions'> }[] }
    const extraction = recorded.results.find(row => row.fixture === 'booking')!.extraction
    const entry = { ...extraction.entries[0], recordId: '1', questions: ['未识别结束当地日期，请对照原图补充确认；不会沿用其他日期或猜测。'] }
    vi.mocked(extractScreenshot).mockResolvedValue({ ...doc, ...extraction, documentKind: 'booking', entries: [entry] })
    vi.mocked(confirmScreenshot).mockRejectedValueOnce(new Error('请确认完整当地日期、开始和结束时间')).mockResolvedValueOnce({ requirements: req, booking: null, notice: '已加入待确认需求' })
    await render(); await upload(); await chooseEntity()
    expect(input('结束当地日期').value).toBe('')
    expect(host.textContent).toContain('未识别结束当地日期')
    await act(async () => host.querySelector<HTMLInputElement>('input[type=checkbox]')!.click())
    await act(async () => button('确认加入需求表单').click())
    expect(adopted).not.toHaveBeenCalled()
    expect(host.textContent).toContain('请确认完整当地日期')
    await change(input('结束当地日期'), '2026-10-25')
    await act(async () => button('确认加入需求表单').click())
    expect(confirmScreenshot).toHaveBeenLastCalledWith(expect.objectContaining({ endDate: '2026-10-25', overridePolarity: true }))
    expect(adopted).toHaveBeenCalledTimes(1)
  })
})
