import { describe, expect, it } from 'vitest'
import type { TripItem } from '../types/itinerary'
import { evidenceLabel, evidenceTone } from './evidence'

const item = (fields: Partial<TripItem> = {}): TripItem => ({ itemType: 'attraction', poiName: '寺院', ...fields })
describe('证据徽标', () => {
  it('缺失字段或只有来源不冒充已核实', () => {
    expect(evidenceLabel(item())).toBe('')
    expect(evidenceLabel(item({ source: 'opentripmap:xid' }))).toBe('')
  })
  it('未核实与过期优先于 observed', () => {
    expect(evidenceLabel(item({ valueKind: 'observed', verificationStatus: 'unverified' }))).toBe('待核实')
    expect(evidenceLabel(item({ valueKind: 'observed', freshnessStatus: 'stale' }))).toBe('来源信息可能过期')
  })
  it('来源和估算都不声称票价已核实', () => {
    expect(evidenceLabel(item({ valueKind: 'observed' }))).toBe('地点有来源')
    expect(evidenceLabel(item({ verificationStatus: 'partially_verified' }))).toBe('部分信息有据')
    expect(evidenceLabel(item({ valueKind: 'estimated' }))).toBe('估算信息')
    expect(evidenceLabel(item({ valueKind: 'generated' }))).toBe('生成信息')
  })
  it('GROUND-2：字段级票在场时按字段说话——地点有据但价格模型直写不背书', () => {
    const ticket = (overrides: { verificationStatus: 'verified' | 'unverified'; valueKind: 'observed' | 'generated' }) => ({
      sourceRef: null,
      sourceUrl: null,
      provider: null,
      retrievedAt: null,
      expiresAt: null,
      verificationStatus: overrides.verificationStatus,
      valueKind: overrides.valueKind,
      freshnessStatus: 'unknown' as const,
      reviewRequirement: 'before_departure' as const,
    })
    const identityTicket = ticket({ verificationStatus: 'verified', valueKind: 'observed' })
    // 地点票 observed+verified、价格票 generated → 合成语义，警示语气
    const withModelCost = item({
      verificationStatus: 'partially_verified',
      factEvidence: { identity: identityTicket, cost: ticket({ verificationStatus: 'unverified', valueKind: 'generated' }) },
    })
    expect(evidenceLabel(withModelCost)).toBe('地点有来源·价格待核实')
    expect(evidenceTone(withModelCost)).toBe('warning')
    // 价格也是 observed（权威行回填）→ 纯「地点有来源」
    const withObservedCost = item({
      verificationStatus: 'partially_verified',
      factEvidence: { identity: identityTicket, cost: ticket({ verificationStatus: 'verified', valueKind: 'observed' }) },
    })
    expect(evidenceLabel(withObservedCost)).toBe('地点有来源')
    expect(evidenceTone(withObservedCost)).toBe('info')
    // 无价格票（免费点/无值）不触发组合文案
    const noCostTicket = item({
      verificationStatus: 'partially_verified',
      factEvidence: { identity: identityTicket },
    })
    expect(evidenceLabel(noCostTicket)).toBe('地点有来源')
  })
  it('语气分类供两套前端共用配色口径', () => {
    expect(evidenceTone(item({ valueKind: 'estimated' }))).toBe('warning')
    expect(evidenceTone(item({ verificationStatus: 'unverified' }))).toBe('warning')
    expect(evidenceTone(item({ freshnessStatus: 'stale' }))).toBe('warning')
    expect(evidenceTone(item({ valueKind: 'generated' }))).toBe('warning')
    expect(evidenceTone(item({ verificationStatus: 'partially_verified' }))).toBe('info')
    expect(evidenceTone(item({ valueKind: 'observed' }))).toBe('info')
    expect(evidenceTone(item())).toBe('')
  })
})