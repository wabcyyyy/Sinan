import { isOfflineError, ReactApiError } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'

/** 模型接入设置页的纯逻辑：表单校验、脱敏展示、通道归约与失败文案。
 * 规则镜像后端 LlmGatewayCreateBody 的 field_validator（app/schemas/business/llm_gateway.py），
 * 前端先拦一道，后端仍是最终裁决。 */

export interface GatewayFormValues {
  name: string
  baseUrl: string
  apiKey: string
  model: string
}

export function emptyGatewayForm(): GatewayFormValues {
  return { name: '', baseUrl: '', apiKey: '', model: '' }
}

/** 编辑表单永远不带密钥：后端只回显尾 4 位，apiKey 留空提交 = 不改密钥。 */
export function formFromGateway(gateway: Contracts.LlmGatewayVO): GatewayFormValues {
  return { name: gateway.name, baseUrl: gateway.baseUrl, apiKey: '', model: gateway.model }
}

/** 校验表单：返回 '' 表示通过，否则是给用户看的错误文案。 */
export function validateGatewayForm(values: GatewayFormValues, isCreate: boolean): string {
  const name = values.name.trim()
  if (!name) return '配置名不能为空'
  if (name.length > 64) return '配置名最长 64 字符'
  const baseUrl = values.baseUrl.trim()
  if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) return '网关地址必须以 http:// 或 https:// 开头'
  if (baseUrl.length > 512) return '网关地址过长'
  const model = values.model.trim()
  if (!model) return '模型名不能为空'
  if (model.length > 128) return '模型名最长 128 字符'
  const apiKey = values.apiKey.trim()
  if (apiKey.length > 256) return 'API 密钥过长'
  if (!apiKey && isCreate) return 'API 密钥不能为空'
  if (apiKey && apiKey.length < 8) return 'API 密钥至少 8 个字符'
  return ''
}

export function createBody(values: GatewayFormValues): Contracts.LlmGatewayCreateBody {
  return {
    name: values.name.trim(),
    baseUrl: values.baseUrl.trim(),
    apiKey: values.apiKey.trim(),
    model: values.model.trim(),
  }
}

/** 编辑提交体：apiKey 留空 → null（后端语义：不改密钥）。 */
export function updateBody(values: GatewayFormValues): Contracts.LlmGatewayUpdateBody {
  const apiKey = values.apiKey.trim()
  return {
    name: values.name.trim(),
    baseUrl: values.baseUrl.trim(),
    model: values.model.trim(),
    apiKey: apiKey || null,
  }
}

/** 脱敏展示：后端只给尾 4 位，前端固定拼 sk-**** 前缀；没有 hint 说明还没存过密钥。 */
export function maskDisplay(hint: string | null): string {
  const tail = (hint || '').trim()
  return tail ? `sk-****${tail}` : '未保存'
}

export type ChannelStatus = { kind: 'default' } | { kind: 'custom'; gateway: Contracts.LlmGatewayVO }

/** 当前生效通道归约：启用互斥由后端保证（同用户至多一条 enabled），这里防御性取第一条。 */
export function activeChannel(items: Contracts.LlmGatewayVO[]): ChannelStatus {
  const enabled = items.find((item) => item.enabled)
  return enabled ? { kind: 'custom', gateway: enabled } : { kind: 'default' }
}

/** 启用归约：目标置 enabled、其余清掉（镜像服务端互斥启用语义）。 */
export function applyEnabledFlag(items: Contracts.LlmGatewayVO[], enabledId: number): Contracts.LlmGatewayVO[] {
  return items.map((item) => ({ ...item, enabled: item.id === enabledId }))
}

export function applyGatewayUpdate(items: Contracts.LlmGatewayVO[], updated: Contracts.LlmGatewayVO): Contracts.LlmGatewayVO[] {
  return items.map((item) => (item.id === updated.id ? updated : item))
}

export function removeGateway(items: Contracts.LlmGatewayVO[], id: number): Contracts.LlmGatewayVO[] {
  return items.filter((item) => item.id !== id)
}

/** 操作失败文案：401 由页面单独走登录引导；离线/5xx 给重试提示；其余透传后端 message（后端文案已脱敏）。 */
export function gatewayFeedbackText(error: unknown): string {
  if (isOfflineError(error)) return '暂时连不上司南服务，稍后再试。'
  if (error instanceof ReactApiError && error.message) return error.message
  return '操作没有成功，请稍后再试。'
}
