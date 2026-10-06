import { useEffect, useState } from 'react'
import type { ChangeEvent, FormEvent } from 'react'
import {
  createLlmGateway,
  deleteLlmGateway,
  disableLlmGateway,
  enableLlmGateway,
  isUnauthorized,
  listLlmGateways,
  testLlmGateway,
  updateLlmGateway,
} from '../../api/sinan'
import type * as Contracts from '../../types/generated/contracts'
import { loginRedirect, navigate } from '../router'
import { Icon } from '../shared/Icon'
import { PageHeader } from '../shared/PageHeader'
import { EmptyBlock, ErrorBlock, LoadingBlock } from '../shared/States'
import type { ChannelStatus, GatewayFormValues } from './llmGateway'
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

/** 模型接入设置页（BYOK）：页面组件只做编排，可静态渲染的展示块（ChannelBanner /
 * GatewayCard / GatewayForm）单独导出并配 llmGateway.test.ts。
 * 密钥红线：输入框用密码型，列表只显示后端回显的尾 4 位（maskDisplay），全页不出现明文。 */

type FormState = { id: number | null; values: GatewayFormValues }
type Note = { kind: 'ok' | 'error'; text: string }

export function SettingsPage() {
  const [items, setItems] = useState<Contracts.LlmGatewayVO[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [authExpired, setAuthExpired] = useState(false)
  const [form, setForm] = useState<FormState | null>(null)
  const [saving, setSaving] = useState(false)
  const [actingId, setActingId] = useState<number | null>(null)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [testResult, setTestResult] = useState<{ id: number; result: Contracts.LlmGatewayTestVO } | null>(null)
  const [deleteArm, setDeleteArm] = useState<number | null>(null)
  const [note, setNote] = useState<Note | null>(null)

  const load = () => {
    setLoading(true); setError(''); setAuthExpired(false); setNote(null)
    listLlmGateways()
      .then((list) => { setItems(list); setLoading(false) })
      .catch((err: unknown) => {
        setItems([])
        if (isUnauthorized(err)) { setAuthExpired(true); setError('登录后才能管理模型接入，请先登录') }
        else setError(err instanceof Error && err.message ? err.message : '配置加载失败，请稍后再试')
        setLoading(false)
      })
  }
  useEffect(() => { load() }, [])

  /** 401 统一走登录引导；其余失败进顶部 note（不吞、有明确反馈）。 */
  const handleOpError = (err: unknown) => {
    if (isUnauthorized(err)) { setAuthExpired(true); setError('登录已失效，请重新登录后管理模型接入'); setNote(null); setLoading(false) }
    else setNote({ kind: 'error', text: gatewayFeedbackText(err) })
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!form) return
    const invalid = validateGatewayForm(form.values, form.id === null)
    if (invalid) { setNote({ kind: 'error', text: invalid }); return }
    setSaving(true)
    try {
      const saved = form.id === null
        ? await createLlmGateway(createBody(form.values))
        : await updateLlmGateway(form.id, updateBody(form.values))
      setItems((prev) => (form.id === null ? [...prev, saved] : applyGatewayUpdate(prev, saved)))
      setForm(null)
      setNote({ kind: 'ok', text: form.id === null ? '配置已保存，点「启用」后生效。' : '修改已保存。' })
    } catch (err) {
      handleOpError(err)
    } finally { setSaving(false) }
  }

  const toggle = async (gateway: Contracts.LlmGatewayVO) => {
    setDeleteArm(null); setNote(null); setActingId(gateway.id)
    try {
      if (gateway.enabled) {
        const updated = await disableLlmGateway(gateway.id)
        setItems((prev) => applyGatewayUpdate(prev, updated))
        setNote({ kind: 'ok', text: `已停用「${gateway.name}」，回到司南默认通道。` })
      } else {
        const updated = await enableLlmGateway(gateway.id)
        setItems((prev) => applyEnabledFlag(applyGatewayUpdate(prev, updated), gateway.id))
        setNote({ kind: 'ok', text: `已启用「${gateway.name}」，之后的生成与对话走你的通道。` })
      }
    } catch (err) { handleOpError(err) } finally { setActingId(null) }
  }

  const test = async (gateway: Contracts.LlmGatewayVO) => {
    setDeleteArm(null); setTestingId(gateway.id); setTestResult(null)
    try {
      const result = await testLlmGateway(gateway.id)
      setTestResult({ id: gateway.id, result })
    } catch (err) {
      setTestResult({ id: gateway.id, result: { ok: false, latencyMs: 0, message: gatewayFeedbackText(err) } })
    } finally { setTestingId(null) }
  }

  /** 删除二次确认：第一次点只亮起「确认删除」，再点才真删；其他任何操作都会解除亮起。 */
  const remove = async (gateway: Contracts.LlmGatewayVO) => {
    if (deleteArm !== gateway.id) { setDeleteArm(gateway.id); setNote(null); return }
    setActingId(gateway.id); setNote(null)
    try {
      await deleteLlmGateway(gateway.id)
      setItems((prev) => removeGateway(prev, gateway.id))
      if (form?.id === gateway.id) setForm(null)
      setNote({ kind: 'ok', text: gateway.enabled ? `已删除「${gateway.name}」，生效通道回到司南默认。` : `已删除「${gateway.name}」。` })
      setDeleteArm(null)
    } catch (err) { handleOpError(err); setDeleteArm(null) } finally { setActingId(null) }
  }

  const channel = activeChannel(items)
  const editing = form && form.id !== null ? items.find((item) => item.id === form.id) ?? null : null

  return <div className="page-content settings-page">
    <PageHeader eyebrow="Model access" title="模型接入" description="接入你的 OpenAI 兼容 API：生成与对话可走你自己的通道与额度。密钥加密保存在你的账号下，任何页面只显示尾 4 位。" />
    {loading ? <LoadingBlock label="正在读取配置…" />
      : error ? <ErrorBlock message={error} onRetry={load} onLogin={authExpired ? () => navigate(loginRedirect()) : undefined} />
        : <>
          <ChannelBanner status={channel} />
          {note && <p className={note.kind === 'ok' ? 'settings-note ok' : 'settings-note error'} role={note.kind === 'ok' ? 'status' : 'alert'}>{note.text}</p>}
          <div className="settings-section-head">
            <h2>我的配置</h2>
            {!form && <button className="button button-secondary" type="button" onClick={() => { setForm({ id: null, values: emptyGatewayForm() }); setNote(null); setDeleteArm(null) }}><Icon name="edit" size={15} />新增配置</button>}
          </div>
          {form && <GatewayForm
            mode={form.id === null ? 'create' : 'edit'}
            title={form.id === null ? '新增配置' : `编辑「${editing?.name ?? '配置'}」`}
            values={form.values}
            hint={editing ? editing.apiKeyHint : null}
            saving={saving}
            onChange={(values) => setForm({ ...form, values })}
            onSubmit={submit}
            onCancel={() => { setForm(null); setNote(null) }}
          />}
          {items.length === 0 && !form
            ? <EmptyBlock title="还没有自有通道" description="不配置也可以直接用司南默认通道规划行程；想用自己的 API 额度时，从「新增配置」接入。" />
            : <div className="gateway-list">
              {items.map((gateway) => <GatewayCard
                key={gateway.id}
                gateway={gateway}
                busy={actingId === gateway.id}
                deleteArmed={deleteArm === gateway.id}
                testing={testingId === gateway.id}
                testResult={testResult?.id === gateway.id ? testResult.result : null}
                onToggle={() => toggle(gateway)}
                onEdit={() => { setDeleteArm(null); setTestResult(null); setNote(null); setForm({ id: gateway.id, values: formFromGateway(gateway) }) }}
                onTest={() => test(gateway)}
                onDelete={() => remove(gateway)}
              />)}
            </div>}
        </>}
  </div>
}

export function ChannelBanner({ status }: { status: ChannelStatus }) {
  const custom = status.kind === 'custom'
  return <div className={custom ? 'channel-banner is-custom' : 'channel-banner'} role="status" aria-live="polite">
    <span className="channel-dot" aria-hidden="true" />
    <div className="channel-copy">
      <strong>当前生效通道</strong>
      {custom
        ? <p>自有通道 · {status.gateway.name}（{status.gateway.model}）—— 生成与对话消耗你配置的 API 额度。</p>
        : <p>司南默认通道 —— 无需任何配置，直接规划行程。</p>}
      {custom && <small>自有通道调用失败时行程生成会直接报错，不会自动回退默认通道；遇到问题先在这里停用或修正配置。</small>}
    </div>
  </div>
}

export function GatewayCard({ gateway, busy, deleteArmed, testing, testResult, onToggle, onEdit, onTest, onDelete }: {
  gateway: Contracts.LlmGatewayVO
  busy: boolean
  deleteArmed: boolean
  testing: boolean
  testResult: Contracts.LlmGatewayTestVO | null
  onToggle: () => void
  onEdit: () => void
  onTest: () => void
  onDelete: () => void
}) {
  const stamp = gateway.updatedAt || gateway.createdAt
  return <article className={gateway.enabled ? 'gateway-card is-enabled' : 'gateway-card'}>
    <header>
      <div>
        <h3>{gateway.name}</h3>
        <p className="gateway-card-url">{gateway.baseUrl}</p>
      </div>
      <span className={gateway.enabled ? 'status-badge status-ready' : 'status-badge status-offline'}>
        <span aria-hidden="true" />{gateway.enabled ? '生效中' : '未启用'}
      </span>
    </header>
    <dl className="gateway-card-meta">
      <div><dt>模型</dt><dd>{gateway.model}</dd></div>
      <div><dt>API 密钥</dt><dd>{maskDisplay(gateway.apiKeyHint)}</dd></div>
      <div><dt>更新时间</dt><dd>{stamp ? new Date(stamp).toLocaleString('zh-CN', { hour12: false }) : '—'}</dd></div>
    </dl>
    {testResult && <p className={testResult.ok ? 'gateway-test ok' : 'gateway-test bad'} role={testResult.ok ? 'status' : 'alert'}>
      {testResult.ok ? `连接成功 · ${testResult.latencyMs}ms` : `连接失败 · ${testResult.message}`}
    </p>}
    <footer className="gateway-card-actions">
      <button className={gateway.enabled ? 'button button-secondary' : 'button button-primary'} type="button" disabled={busy || testing} onClick={onToggle}>
        {gateway.enabled ? '停用' : '启用'}
      </button>
      <button className="button button-secondary" type="button" disabled={busy || testing} onClick={onEdit}><Icon name="edit" size={14} />编辑</button>
      <button className="button button-secondary" type="button" disabled={busy} onClick={onTest}>{testing ? '测试中…' : '测试连接'}</button>
      <button className={deleteArmed ? 'gateway-delete armed' : 'gateway-delete'} type="button" disabled={busy} onClick={onDelete}>
        {deleteArmed ? '确认删除' : '删除'}
      </button>
    </footer>
  </article>
}

export function GatewayForm({ mode, title, values, hint, saving, onChange, onSubmit, onCancel }: {
  mode: 'create' | 'edit'
  title: string
  values: GatewayFormValues
  hint: string | null
  saving: boolean
  onChange: (values: GatewayFormValues) => void
  onSubmit: (event: FormEvent) => void
  onCancel: () => void
}) {
  const field = (key: keyof GatewayFormValues) => (event: ChangeEvent<HTMLInputElement>) =>
    onChange({ ...values, [key]: event.target.value })
  return <form className="gateway-form" onSubmit={onSubmit} noValidate>
    <h3>{title}</h3>
    <label>配置名<input value={values.name} onChange={field('name')} maxLength={64} placeholder="例如：我的通义千问" /></label>
    <label>网关地址（OpenAI 兼容 Base URL）<input value={values.baseUrl} onChange={field('baseUrl')} maxLength={512} inputMode="url" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" /></label>
    <label>API 密钥
      <input
        type="password"
        autoComplete="new-password"
        value={values.apiKey}
        onChange={field('apiKey')}
        maxLength={256}
        placeholder={mode === 'create' ? 'sk-…（保存后不再回显）' : '留空表示不修改已保存的密钥'}
      />
      {mode === 'edit' && <span className="gateway-form-hint">当前密钥 {maskDisplay(hint)}，留空保持不变</span>}
    </label>
    <label>模型名<input value={values.model} onChange={field('model')} maxLength={128} placeholder="例如：qwen-plus" /></label>
    <div className="gateway-form-actions">
      <button className="button button-primary" type="submit" disabled={saving}>{saving ? '正在保存…' : mode === 'create' ? '保存配置' : '保存修改'}<Icon name="check" size={15} /></button>
      <button className="button button-secondary" type="button" onClick={onCancel} disabled={saving}>取消</button>
    </div>
    <p className="gateway-form-note">密钥加密保存在你的账号下，仅在调用你的网关时使用；列表与日志里只出现尾 4 位。</p>
  </form>
}
