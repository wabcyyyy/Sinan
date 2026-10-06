/**
 * 证据徽章映射的单一真源。
 *
 * 此前这份映射只存在于旧组件树，详情页曾自己写了一套
 * `valueKind` 三元表达式——同一后端契约字段（verificationStatus/valueKind/
 * freshnessStatus）在两个前端渲染出不同口径（审查 P1-4 的"双前端口径漂移"）。
 * 本模块只依赖类型形状，不 import 任何框架，渲染层从这里取。
 *
 * 判定顺序即语义优先级：过期 > 未核实 > 部分有据 > （有票）有来源 > 估算/生成。
 */

/** 只声明本模块真正读取的字段形状(结构化类型，调用方按形状传入)。 */
export interface EvidenceShape {
  verificationStatus?: string | null
  freshnessStatus?: string | null
  valueKind?: string | null
  factEvidence?: {
    identity?: { valueKind?: string | null; verificationStatus?: string | null } | null
    cost?: { valueKind?: string | null; verificationStatus?: string | null } | null
  } | null
}

export function evidenceLabel(item: EvidenceShape): string {
  if (item.freshnessStatus === 'stale') return '来源信息可能过期'
  if (item.verificationStatus === 'unverified') return '待核实'
  // GROUND-2：字段级票在场时按字段说话——地点有据但票价是模型直写时，
  // 不再让行级「部分信息有据」把价格一起背书
  const identity = item.factEvidence?.identity
  const cost = item.factEvidence?.cost
  if (identity?.valueKind === 'observed' && identity.verificationStatus === 'verified') {
    if (cost && cost.valueKind !== 'observed') return '地点有来源·价格待核实'
    return '地点有来源'
  }
  if (item.verificationStatus === 'partially_verified') return '部分信息有据'
  if (item.valueKind === 'estimated') return '估算信息'
  if (item.valueKind === 'generated') return '生成信息'
  if (item.valueKind === 'observed') return '地点有来源'
  return ''
}

/** 徽章语气：两个前端据此决定配色，避免各自再定义一组"什么算警示"。 */
export type EvidenceTone = 'warning' | 'info' | ''

export function evidenceTone(item: EvidenceShape): EvidenceTone {
  const label = evidenceLabel(item)
  if (!label) return ''
  if (label === '待核实' || label === '来源信息可能过期' || label === '估算信息' || label === '生成信息' || label === '地点有来源·价格待核实') {
    return 'warning'
  }
  return 'info'
}