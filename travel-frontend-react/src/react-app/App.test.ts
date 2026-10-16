import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from './App'
import { getUserInfo, logout, ReactApiError } from '../api/sinan'
import { activateIntakeOwner, intakeSessionStorage, intakeHistoryStorage, suspendIntakeOwner } from './home/intakeStorage'
import type { UserInfoVO } from '../types/generated/contracts'

vi.mock('../api/sinan', async (original) => ({ ...await original<typeof import('../api/sinan')>(), getUserInfo: vi.fn(), logout: vi.fn() }))
vi.mock('./home/HomePage', () => ({ HomePage: () => createElement('div', { 'data-private': true }, intakeSessionStorage.getItem('current') ?? 'empty') }))
vi.mock('./layout/AppShell', () => ({ AppShell: ({ children, onLogout }: { children: React.ReactNode, onLogout: () => void }) => createElement('div', null, createElement('button', { onClick: onLogout }, 'logout'), children) }))
vi.mock('./auth/LoginPage', () => ({ LoginPage: ({ onAuthenticated }: { onAuthenticated: (info: UserInfoVO) => void }) => createElement('button', { onClick: () => onAuthenticated({ id: 3, username: 'C' } as UserInfoVO) }, 'login') }))

let container: HTMLDivElement
let root: Root
const user = (id: number): UserInfoVO => ({ id, username: `user${id}` } as UserInfoVO)
const flush = async () => { await act(async () => { await Promise.resolve() }) }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  localStorage.clear(); sessionStorage.clear(); suspendIntakeOwner()
  window.history.replaceState({}, '', '/')
  container = document.createElement('div'); document.body.appendChild(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove() })

it('waits for server identity and ignores a cached display name', async () => {
  localStorage.setItem('sinan-username', 'A')
  sessionStorage.setItem('sinan-intake-v1', 'legacy private')
  activateIntakeOwner(1); intakeSessionStorage.setItem('current', 'A private'); suspendIntakeOwner()
  let resolve!: (info: UserInfoVO) => void
  vi.mocked(getUserInfo).mockImplementation(() => new Promise((done) => { resolve = done }))
  await act(async () => root.render(createElement(App)))
  expect(container.textContent).toContain('正在确认')
  expect(container.querySelector('[data-private]')).toBeNull()
  await act(async () => resolve(user(2)))
  expect(container.textContent).not.toContain('A private')
  expect(sessionStorage.getItem('sinan-intake-v1')).toBeNull()
})

it('masks private content during a failed logout and supports retry', async () => {
  vi.mocked(getUserInfo).mockResolvedValue(user(1))
  await act(async () => root.render(createElement(App)))
  intakeSessionStorage.setItem('current', 'secret')
  vi.mocked(logout).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined)
  await act(async () => container.querySelector('button')!.click())
  expect(container.textContent).toContain('退出未完成')
  expect(container.querySelector('[data-private]')).toBeNull()
  await act(async () => container.querySelector('button')!.click())
  expect(container.textContent).toContain('empty')
  expect(vi.mocked(logout)).toHaveBeenCalledTimes(2)
})

it('storage broadcasts re-confirm identity and discard an earlier response', async () => {
  let first!: (info: UserInfoVO) => void
  vi.mocked(getUserInfo).mockImplementationOnce(() => new Promise((done) => { first = done })).mockResolvedValueOnce(user(2))
  await act(async () => root.render(createElement(App)))
  await act(async () => window.dispatchEvent(new StorageEvent('storage', { key: 'sinan-auth-change', newValue: 'B' })))
  await act(async () => first(user(1)))
  intakeSessionStorage.setItem('current', 'B draft')
  expect(sessionStorage.getItem('sinan-intake-v2:user-2:current')).toBe('B draft')
  expect(sessionStorage.getItem('sinan-intake-v2:user-1:current')).toBeNull()
})

it('first login continues only this tab anonymous conversation', async () => {
  vi.mocked(getUserInfo).mockRejectedValue(new ReactApiError('unauthorized', 401, 401))
  window.history.replaceState({}, '', '/login')
  await act(async () => root.render(createElement(App)))
  await flush()
  intakeSessionStorage.setItem('current', 'this conversation')
  intakeHistoryStorage.setItem('history', 'anonymous history')
  await act(async () => container.querySelector('button')!.click())
  expect(intakeSessionStorage.getItem('current')).toBe('this conversation')
  expect(intakeHistoryStorage.getItem('history')).toBeNull()
})


it('opens the full demo without session bootstrap or touching real identity and drafts', async () => {
  window.history.replaceState({}, '', '/demo')
  localStorage.setItem('sinan-username', 'real-user')
  sessionStorage.setItem('sinan-intake-v1', 'legacy real draft')
  sessionStorage.setItem('sinan-intake-v2:user-7:current', 'real draft')
  await act(async () => root.render(createElement(App)))
  expect(container.textContent).toContain('产品交互演示')
  expect(container.textContent).toContain('演示访客')
  expect(vi.mocked(getUserInfo)).not.toHaveBeenCalled()
  expect(localStorage.getItem('sinan-username')).toBe('real-user')
  expect(sessionStorage.getItem('sinan-intake-v1')).toBe('legacy real draft')
  expect(sessionStorage.getItem('sinan-intake-v2:user-7:current')).toBe('real draft')
})
