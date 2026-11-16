// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { App } from './App'
import { useAuthStore } from '../stores/auth'
import type { Role } from '../api/types'

let root: Root
let host: HTMLDivElement
let roles: Role[]
let logged: boolean
let mode: 'dev' | 'prod'
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 50; index++) { await settle(); if (condition()) return }
  throw new Error('界面未在预期时间内完成更新')
}
beforeEach(() => {
  localStorage.clear()
  roles = ['uploader']; logged = false; mode = 'dev'
  useAuthStore.setState({ user: null, initialized: false, notice: '' })
  window.history.replaceState({}, '', '/admin/roles')
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.open = true } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.open = false } })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/health') return response({ version: '0.1.0', mode, database_available: true })
    if (url === '/api/auth/logout') { logged = false; return response({ message: '已退出登录' }) }
    if (url === '/api/auth/me') return logged ? response({ id: 1, username: 'demo', display_name: null, roles, enabled: true }) : response({ code: 'login_required', message: '请先登录' }, 401)
    return response([])
  }))
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('未登录访问深层路径跳登录页', async () => {
  act(() => root.render(<App />)); await settle()
  expect(window.location.pathname).toBe('/login')
  expect(host.textContent).toContain('登录工作台')
  expect(host.querySelector('input[type="password"]')).not.toBeNull()
})
it('上传人导航不显示管理、审核，退出后深层页面回到登录', async () => {
  logged = true; window.history.replaceState({}, '', '/')
  act(() => root.render(<App />)); await settle()
  const labels = [...host.querySelectorAll('nav a')].map(node => node.textContent)
  expect(labels).toContain('上传'); expect(labels).not.toContain('管理'); expect(labels).not.toContain('待初审')
  const exit = [...host.querySelectorAll('button')].find(node => node.textContent === '退出')!
  await act(async () => exit.click()); await settle()
  expect(window.location.pathname).toBe('/login')
  expect(useAuthStore.getState().user).toBeNull()
})
it('组件样例包含 60 张卡片、展示弹窗且轻量偏好可切换', async () => {
  logged = true; window.history.replaceState({}, '', '/components')
  act(() => root.render(<App />)); await until(() => host.querySelectorAll('.grid > .glass-surface').length === 60)
  expect(host.querySelectorAll('.grid > .glass-surface')).toHaveLength(60)
  const open = [...host.querySelectorAll('button')].find(node => node.textContent === '打开弹窗')!
  act(() => open.click())
  expect(document.querySelector('dialog')?.open).toBe(true)
  const toggle = host.querySelector<HTMLButtonElement>('[aria-label="切换视觉模式"]')!
  act(() => toggle.click())
  expect(localStorage.getItem('nas-workbench-visual-mode')).toBe('lite')
})
it('生产模式不显示组件入口且直接访问也重定向首页', async () => {
  logged = true; mode = 'prod'; window.history.replaceState({}, '', '/components')
  act(() => root.render(<App />)); await until(() => window.location.pathname === '/')
  expect(window.location.pathname).toBe('/')
  expect([...host.querySelectorAll('nav a')].map(node => node.textContent)).not.toContain('组件样例')
})
