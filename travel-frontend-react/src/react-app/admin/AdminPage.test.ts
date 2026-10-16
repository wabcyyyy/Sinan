import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AdminPage } from './AdminPage'
import { getUserInfo, getTrialUsers, getUserWallet, issueTrialInvites, grantTrialCredit } from '../../api/sinan'

vi.mock('../../api/sinan', () => ({
  getUserInfo: vi.fn(), getTrialUsers: vi.fn(), getUserWallet: vi.fn(), issueTrialInvites: vi.fn(),
  grantTrialCredit: vi.fn(), setTrialUserStatus: vi.fn(), reconcileTrialCharge: vi.fn(),
}))
let root: Root
let container: HTMLDivElement
beforeEach(() => {
  ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.clearAllMocks() })
async function mount() { await act(async () => { root.render(createElement(AdminPage)) }) }
function button(label: string) { return [...container.querySelectorAll('button')].find((node) => node.textContent === label) as HTMLButtonElement }
const user = { id: 1, username: 'operator', nickname: null, phone: null, role: 'admin' }
const wallet = { userId: 2, balanceCny: 6, spentCny: 0, entries: [] }

it('普通用户看不到管理操作，且不会请求用户列表', async () => {
  vi.mocked(getUserInfo).mockResolvedValue({ ...user, role: 'user' })
  await mount()
  expect(container.textContent).toContain('仅管理员')
  expect(getTrialUsers).not.toHaveBeenCalled()
  expect(container.querySelector('button')).toBeNull()
})

it('管理员生成一次性邀请码；赠额必须填写原因且携带幂等标识', async () => {
  vi.mocked(getUserInfo).mockResolvedValue(user)
  vi.mocked(getTrialUsers).mockResolvedValue({ total: 1, records: [{ id: 2, username: 'tester', status: 1, role: 'user' }] })
  vi.mocked(issueTrialInvites).mockResolvedValue({ codes: ['one-time-example'] })
  vi.mocked(getUserWallet).mockResolvedValue(wallet)
  vi.mocked(grantTrialCredit).mockResolvedValue({ ...wallet, balanceCny: 12 })
  await mount()
  await act(async () => button('生成一次性邀请码').click())
  expect(container.querySelector('output')?.textContent).toBe('one-time-example')
  await act(async () => button('查看余额与费用').click())
  expect(button('确认赠额').disabled).toBe(true)
  const input = container.querySelector('input[maxlength="200"]') as HTMLInputElement
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, '延长试用')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => button('确认赠额').click())
  expect(grantTrialCredit).toHaveBeenCalledWith(2, { amountCny: 6, reason: '延长试用', requestId: expect.any(String) })
  expect(container.textContent).toContain('12.0000')
})
