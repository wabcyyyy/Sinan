import { act } from 'react'
import { createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createLlmGateway, listLlmGateways, ReactApiError } from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import { ChannelBanner, GatewayCard, GatewayForm, SettingsPage } from './SettingsPage'
import {
  activeChannel,
  applyEnabledFlag,
  applyGatewayUpdate,
  createBody,
  emptyGatewayForm,
  formFromGateway,
  gatewayFeedbackText,
  maskDisplay,
  removeGateway,
  updateBody,
  validateGatewayForm,
} from './llmGateway'

/** 设置页测试：纯函数（校验/脱敏/归约）+ 展示块静态渲染 + 保存链路交互。
 * 交互用例 mock 掉 api 层（真 createRoot + act，零新测试依赖），与 chatComposer.test.ts 同纪律。 */

vi.mock('../../api/sinan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sinan')>()
  return { ...actual, listLlmGateways: vi.fn(), createLlmGateway: vi.fn() }
})

const listMock = vi.mocked(listLlmGateways)
const createMock = vi.mocked(createLlmGateway)

const sampleGateway: Contracts.LlmGatewayVO = {
  id: 7,
  name: '我的通义',
  baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  model: 'qwen-plus',
  apiKeyHint: '9f2a',
  enabled: true,
  createdAt: '2026-10-01T10:00:00',
  updatedAt: '2026-10-01T10:00:00',
}

const validValues = {
  name: '我的通义',
  baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  apiKey: 'sk-live-1234567890abcd',
  model: 'qwen-plus',
}

describe('validateGatewayForm（表单校验）', () => {
  it('合法输入通过', () => {
    expect(validateGatewayForm(validValues, true)).toBe('')
  })
  it('配置名：空/超长分别报错', () => {
    expect(validateGatewayForm({ ...validValues, name: '   ' }, true)).toBe('配置名不能为空')
    expect(validateGatewayForm({ ...validValues, name: 'x'.repeat(65) }, true)).toBe('配置名最长 64 字符')
  })
  it('网关地址：必须 http(s) 开头', () => {
    expect(validateGatewayForm({ ...validValues, baseUrl: 'dashscope.aliyuncs.com/v1' }, true)).toBe('网关地址必须以 http:// 或 https:// 开头')
    expect(validateGatewayForm({ ...validValues, baseUrl: 'ftp://example.com' }, true)).toBe('网关地址必须以 http:// 或 https:// 开头')
  })
  it('模型名：空报错', () => {
    expect(validateGatewayForm({ ...validValues, model: '' }, true)).toBe('模型名不能为空')
  })
  it('密钥：创建必填且 ≥8 字符；编辑留空合法（=不改）', () => {
    expect(validateGatewayForm({ ...validValues, apiKey: '' }, true)).toBe('API 密钥不能为空')
    expect(validateGatewayForm({ ...validValues, apiKey: 'sk-123' }, true)).toBe('API 密钥至少 8 个字符')
    expect(validateGatewayForm({ ...validValues, apiKey: '' }, false)).toBe('')
    expect(validateGatewayForm({ ...validValues, apiKey: 'sk-123' }, false)).toBe('API 密钥至少 8 个字符')
  })
})

describe('脱敏展示与提交体', () => {
  it('maskDisplay：尾 4 位拼 sk-**** 前缀，空值显示未保存', () => {
    expect(maskDisplay('9f2a')).toBe('sk-****9f2a')
    expect(maskDisplay(null)).toBe('未保存')
    expect(maskDisplay('')).toBe('未保存')
  })
  it('formFromGateway 不带密钥（编辑表单从不出示明文/密文）', () => {
    expect(formFromGateway(sampleGateway)).toEqual({ name: '我的通义', baseUrl: sampleGateway.baseUrl, apiKey: '', model: 'qwen-plus' })
  })
  it('createBody/updateBody：字段裁剪空白，编辑空密钥归一为 null', () => {
    expect(createBody({ ...validValues, name: '  我的通义  ' }).name).toBe('我的通义')
    expect(updateBody({ ...validValues, apiKey: '   ' }).apiKey).toBeNull()
    expect(updateBody({ ...validValues, apiKey: ' sk-new-98765432 ' }).apiKey).toBe('sk-new-98765432')
  })
})

describe('通道与列表归约', () => {
  const other = { ...sampleGateway, id: 8, name: '备用', enabled: false }
  it('activeChannel：无启用项=默认通道；有启用项=自有通道', () => {
    expect(activeChannel([other])).toEqual({ kind: 'default' })
    expect(activeChannel([])).toEqual({ kind: 'default' })
    expect(activeChannel([other, sampleGateway])).toEqual({ kind: 'custom', gateway: sampleGateway })
  })
  it('applyEnabledFlag：启用互斥（其余清掉 enabled）', () => {
    const next = applyEnabledFlag([sampleGateway, other], other.id)
    expect(next.map((item) => [item.id, item.enabled])).toEqual([[7, false], [8, true]])
  })
  it('applyGatewayUpdate 按 id 替换；removeGateway 过滤', () => {
    const updated = { ...other, model: 'qwen-max' }
    expect(applyGatewayUpdate([sampleGateway, other], updated)[1].model).toBe('qwen-max')
    expect(removeGateway([sampleGateway, other], 7)).toHaveLength(1)
  })
  it('gatewayFeedbackText：离线给重试提示，其余透传后端 message', () => {
    expect(gatewayFeedbackText(new TypeError('fetch failed'))).toBe('暂时连不上司南服务，稍后再试。')
    expect(gatewayFeedbackText(new ReactApiError('同名网关配置已存在，请换一个名字', 409))).toBe('同名网关配置已存在，请换一个名字')
    expect(gatewayFeedbackText(new Error(''))).toBe('操作没有成功，请稍后再试。')
  })
})

describe('展示块静态渲染', () => {
  it('ChannelBanner：默认/自有两态文案与「当前生效通道」标签', () => {
    const def = renderToStaticMarkup(createElement(ChannelBanner, { status: { kind: 'default' } }))
    expect(def).toContain('当前生效通道')
    expect(def).toContain('司南默认通道')
    const custom = renderToStaticMarkup(createElement(ChannelBanner, { status: { kind: 'custom', gateway: sampleGateway } }))
    expect(custom).toContain('自有通道 · 我的通义（qwen-plus）')
    expect(custom).toContain('不会自动回退默认通道')
  })
  it('GatewayCard：只显示脱敏密钥（sk-****尾4位），不含明文形态', () => {
    const html = renderToStaticMarkup(createElement(GatewayCard, {
      gateway: sampleGateway, busy: false, deleteArmed: false, testing: false, testResult: null,
      onToggle: () => {}, onEdit: () => {}, onTest: () => {}, onDelete: () => {},
    }))
    expect(html).toContain('sk-****9f2a')
    expect(html).toContain('生效中')
    expect(html).toContain('qwen-plus')
    expect(html).toContain(sampleGateway.baseUrl)
    // VO 契约只带 apiKeyHint（尾 4 位）：渲染面从不接触明文密钥字段
    expect(Object.keys(sampleGateway)).toEqual(expect.arrayContaining(['apiKeyHint']))
    expect(Object.keys(sampleGateway)).not.toContain('apiKey')
  })
  it('GatewayCard：删除二次确认亮起与测试结果展示', () => {
    const armed = renderToStaticMarkup(createElement(GatewayCard, {
      gateway: sampleGateway, busy: false, deleteArmed: true, testing: false, testResult: null,
      onToggle: () => {}, onEdit: () => {}, onTest: () => {}, onDelete: () => {},
    }))
    expect(armed).toContain('确认删除')
    const tested = renderToStaticMarkup(createElement(GatewayCard, {
      gateway: { ...sampleGateway, enabled: false }, busy: false, deleteArmed: false, testing: false,
      testResult: { ok: false, latencyMs: 1820, message: '连接超时' },
      onToggle: () => {}, onEdit: () => {}, onTest: () => {}, onDelete: () => {},
    }))
    expect(tested).toContain('连接失败 · 连接超时')
    expect(tested).toContain('未启用')
  })
  it('GatewayForm：密钥输入是密码型且不回显；编辑态给「留空不改」占位与当前尾 4 位', () => {
    const create = renderToStaticMarkup(createElement(GatewayForm, {
      mode: 'create', title: '新增配置', values: emptyGatewayForm(), hint: null, saving: false,
      onChange: () => {}, onSubmit: () => {}, onCancel: () => {},
    }))
    expect(create).toContain('type="password"')
    // renderToStaticMarkup 保留 React 的 camelCase 属性名（autoComplete）
    expect(create).toContain('autoComplete="new-password"')
    expect(create).toContain('保存配置')
    const edit = renderToStaticMarkup(createElement(GatewayForm, {
      mode: 'edit', title: '编辑「我的通义」', values: formFromGateway(sampleGateway), hint: '9f2a', saving: false,
      onChange: () => {}, onSubmit: () => {}, onCancel: () => {},
    }))
    expect(edit).toContain('留空表示不修改已保存的密钥')
    expect(edit).toContain('当前密钥 sk-****9f2a，留空保持不变')
    expect(edit).toContain('保存修改')
  })
  it('SettingsPage 初始渲染：页头与读取态（静态渲染不跑 effect，停在 loading）', () => {
    const html = renderToStaticMarkup(createElement(SettingsPage))
    expect(html).toContain('模型接入')
    expect(html).toContain('正在读取配置')
  })
})

/** 保存链路交互：mock api 层，驱动真实组件状态机。 */
const roots: Array<{ root: Root; container: HTMLDivElement }> = []

/** 异步链（effect 里的 load、提交后的 promise）落进状态时补一个空 act 窗口，避免 act 告警。 */
async function flush() {
  await act(async () => {})
}

async function mountSettings() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  roots.push({ root, container })
  await act(async () => { root.render(createElement(SettingsPage)) })
  await flush()
  return container
}

/** happy-dom 下写受控 input：走原型 value setter 触发 React 的 onChange。 */
function setNativeValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

async function fillForm(container: HTMLDivElement, overrides: Partial<typeof validValues> = {}) {
  const inputs = container.querySelectorAll<HTMLInputElement>('.gateway-form input')
  const values = { ...validValues, ...overrides }
  await act(async () => {
    setNativeValue(inputs[0], values.name)
    setNativeValue(inputs[1], values.baseUrl)
    setNativeValue(inputs[2], values.apiKey)
    setNativeValue(inputs[3], values.model)
  })
  return inputs
}

describe('SettingsPage 保存链路（mock api）', () => {
  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
  })

  afterEach(async () => {
    for (const { root, container } of roots.splice(0)) {
      await act(async () => { root.unmount() })
      container.remove()
    }
    vi.clearAllMocks()
  })

  it('未登录（401）：给出重新登录入口', async () => {
    listMock.mockRejectedValue(new ReactApiError('未登录或登录已过期', 401))
    const container = await mountSettings()
    expect(container.textContent).toContain('登录后才能管理模型接入')
    expect(container.querySelector('.error-block .button-primary')?.textContent).toContain('重新登录')
  })

  it('校验失败：不发请求，就地给出错误反馈', async () => {
    listMock.mockResolvedValue([])
    createMock.mockResolvedValue(sampleGateway)
    const container = await mountSettings()
    await act(async () => { (container.querySelector('.settings-section-head button') as HTMLButtonElement).click() })
    await fillForm(container, { baseUrl: 'dashscope.aliyuncs.com/v1' })
    await act(async () => { (container.querySelector('.gateway-form button[type="submit"]') as HTMLButtonElement).click() })
    const note = container.querySelector('.settings-note')
    expect(note?.getAttribute('role')).toBe('alert')
    expect(note?.textContent).toBe('网关地址必须以 http:// 或 https:// 开头')
    expect(createMock).not.toHaveBeenCalled()
  })

  it('保存成功：表单收起、出现成功反馈与脱敏卡片（无明文密钥）', async () => {
    listMock.mockResolvedValue([])
    createMock.mockResolvedValue({ ...sampleGateway, id: 8, enabled: false, apiKeyHint: 'abcd' })
    const container = await mountSettings()
    await act(async () => { (container.querySelector('.settings-section-head button') as HTMLButtonElement).click() })
    await fillForm(container)
    await act(async () => { (container.querySelector('.gateway-form button[type="submit"]') as HTMLButtonElement).click() })
    expect(createMock).toHaveBeenCalledWith(createBody(validValues))
    const note = container.querySelector('.settings-note')
    expect(note?.getAttribute('role')).toBe('status')
    expect(note?.textContent).toContain('配置已保存')
    expect(container.querySelector('.gateway-form')).toBeNull()
    expect(container.querySelector('.gateway-card')?.textContent).toContain('sk-****abcd')
    expect(container.textContent).not.toContain(validValues.apiKey)
  })

  it('保存失败：409 等后端错误给出明确反馈，表单保留可改', async () => {
    listMock.mockResolvedValue([])
    createMock.mockRejectedValue(new ReactApiError('同名网关配置已存在，请换一个名字', 409))
    const container = await mountSettings()
    await act(async () => { (container.querySelector('.settings-section-head button') as HTMLButtonElement).click() })
    await fillForm(container)
    await act(async () => { (container.querySelector('.gateway-form button[type="submit"]') as HTMLButtonElement).click() })
    const note = container.querySelector('.settings-note')
    expect(note?.getAttribute('role')).toBe('alert')
    expect(note?.textContent).toBe('同名网关配置已存在，请换一个名字')
    expect(container.querySelector('.gateway-form')).not.toBeNull()
  })

  it('列表加载后展示通道横幅：启用的配置即为当前生效通道', async () => {
    listMock.mockResolvedValue([sampleGateway, { ...sampleGateway, id: 8, name: '备用', enabled: false }])
    const container = await mountSettings()
    expect(container.querySelector('.channel-banner')?.textContent).toContain('自有通道 · 我的通义（qwen-plus）')
    expect(container.querySelectorAll('.gateway-card')).toHaveLength(2)
  })
})
