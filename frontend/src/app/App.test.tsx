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
let dsmVerifySsl: boolean
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 50; index++) { await settle(); if (condition()) return }
  throw new Error('界面未在预期时间内完成更新')
}
beforeEach(() => {
  localStorage.clear()
  roles = ['uploader']; logged = false; mode = 'dev'; dsmVerifySsl = true
  useAuthStore.setState({ user: null, initialized: false, notice: '' })
  window.history.replaceState({}, '', '/admin/roles')
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.open = true } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.open = false } })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.startsWith('/api/projects?')) return response({ items: [], total: 0, page: 1, page_size: 20 })
    if (url === '/api/health') return response({ version: '0.1.0', mode, database_available: true, dsm_verify_ssl: dsmVerifySsl })
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
  expect(host.textContent).not.toContain('DSM 证书验证已关闭')
})
it.each([false, true])('证书验证关闭时明确显示测试提示（已登录 %s）', async (isSignedIn) => {
  logged = isSignedIn; mode = 'prod'; dsmVerifySsl = false
  window.history.replaceState({}, '', '/')
  act(() => root.render(<App />))
  const notice = '本机测试环境：DSM 证书验证已关闭'
  await until(() => host.textContent!.includes(notice))
  expect([...host.querySelectorAll('[role="status"]')].some(node => node.textContent?.trim() === notice)).toBe(true)
  expect(window.location.pathname).toBe(isSignedIn ? '/' : '/login')
})
it('上传人导航不显示管理、审核，退出后深层页面回到登录', async () => {
  logged = true; window.history.replaceState({}, '', '/')
  act(() => root.render(<App />)); await settle()
  const labels = [...host.querySelectorAll('nav a')].map(node => node.textContent)
  expect(labels).toContain('资产入库'); expect(labels).not.toContain('管理'); expect(labels).not.toContain('待初审')
  const exit = [...host.querySelectorAll('button')].find(node => node.textContent === '退出')!
  await act(async () => exit.click()); await settle()
  expect(window.location.pathname).toBe('/login')
  expect(useAuthStore.getState().user).toBeNull()
})
it('开发模式的组件样例支持通过弹窗和关闭按钮检查键盘焦点', async () => {
  logged = true; window.history.replaceState({}, '', '/components')
  act(() => root.render(<App />)); await until(() => host.textContent!.includes('组件样例'))
  const open = [...host.querySelectorAll('button')].find(node => node.textContent === '打开弹窗')
  expect(open).toBeDefined()
  open?.focus()
  act(() => open?.click())
  expect(document.querySelector('dialog')?.open).toBe(true)
  const close = document.querySelector('dialog button')
  act(() => (close as HTMLButtonElement).click())
  expect(document.querySelector('dialog')).toBeNull()
  expect(document.activeElement).toBe(open)
})
it('生产模式不显示组件入口且直接访问也重定向首页', async () => {
  logged = true; mode = 'prod'; window.history.replaceState({}, '', '/components')
  act(() => root.render(<App />)); await until(() => window.location.pathname === '/')
  expect(window.location.pathname).toBe('/')
  expect([...host.querySelectorAll('nav a')].map(node => node.textContent)).not.toContain('组件样例')
})

it('会话失效后换账号，不展示前一个账号缓存的批次详情', async () => {
  logged = true; window.history.replaceState({}, '', '/batches/1')
  const originalFetch = fetch
  let batchReads = 0
  vi.stubGlobal('fetch', vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/batches/1') {
      batchReads++
      if (batchReads === 1) return Promise.resolve(response({ id: 1, batch_no: 'B1', folder_name: '前一账号的私有批次', status: '待核对', files: [], products: [], uploader_id: 1 }))
      return new Promise<Response>(() => undefined)
    }
    return originalFetch(url, options)
  }))
  act(() => root.render(<App />))
  await until(() => host.textContent!.includes('前一账号的私有批次'))
  act(() => useAuthStore.getState().clear('登录已过期'))
  await until(() => window.location.pathname === '/login')
  act(() => useAuthStore.getState().setUser({ id: 2, username: 'other', display_name: null, roles: ['uploader'], enabled: true }))
  await until(() => window.location.pathname === '/')
  act(() => { window.history.pushState({}, '', '/batches/1'); window.dispatchEvent(new PopStateEvent('popstate')) })
  await until(() => batchReads === 2)
  expect(host.textContent).not.toContain('前一账号的私有批次')
  expect(host.textContent).toContain('正在加载批次')
})
