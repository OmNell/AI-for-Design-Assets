// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { NasSettingsPage } from './NasSettingsPage'
import { useAuthStore } from '../stores/auth'

let host: HTMLElement
let root: Root
let query: QueryClient
const revision = '9'.repeat(64)
const candidate = { host: '192.168.1.2', dsm_port: 5001, share: '设计资产共享', work_root: '部门',
  system_user: 'wb-system', has_system_password: true, certificate: 'system_ca', regions: { formal: '/部门/设计资产' } }
const active = { ...candidate, work_root: null, regions: { formal: '/设计资产' } }
const view = { active, pending: null, revision, state: 'active', mode: 'dev', message: '当前运行配置', connection_checks: [] }
const accepted = { eligible: true, checks: [{ name: '系统账号 · 备份区', state: 'passed', message: '目录授权符合预期' }],
  changes: ['工作目录'], conflicts: [], regions: candidate.regions }

function button(value: string, parent: ParentNode = host) {
  return [...parent.querySelectorAll('button')].find(node => node.textContent?.trim() === value) as HTMLButtonElement
}

function field(value: string) {
  return [...host.querySelectorAll('label')].find(node => node.textContent?.startsWith(value))!.querySelector('input')!
}

async function until(condition: () => boolean) {
  for (let i = 0; i < 60; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)) })
    if (condition()) return
  }
  throw new Error('NAS 设置界面未完成预期更新')
}

function input(field: HTMLInputElement, value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.open = true } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.open = false } })
  useAuthStore.getState().setUser({ id: 1, username: 'admin', display_name: null, roles: ['admin'], enabled: true })
  host = document.createElement('section')
  document.body.append(host)
  root = createRoot(host)
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  act(() => root.unmount())
  host.remove()
  query.clear()
  useAuthStore.getState().clear()
  vi.unstubAllGlobals()
})

const mount = () => act(() => root.render(<QueryClientProvider client={query}><MemoryRouter><NasSettingsPage /></MemoryRouter></QueryClientProvider>))
const reply = (value: unknown) => new Response(JSON.stringify(value), { status: 200 })

it('目录读取、校验、二次确认和待重启状态', async () => {
  const calls: [string, Record<string, unknown> | null][] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    const body = typeof options?.body === 'string' ? JSON.parse(options.body) : null
    calls.push([url, body])
    switch (url.split('/').at(-1)) {
      case 'browse': return reply({ path: body.path, nodes: body.path ? [] : ['部门'], truncated: false })
      case 'validate': return reply(accepted)
      case 'apply': return reply({ ...view, state: 'pending_restart', pending: candidate, message: '等待运维重启' })
      default: return reply(view)
    }
  }))
  mount()
  await until(() => !!button('检查配置'))
  expect(button('保存配置').disabled).toBe(true)
  input(field('当前管理员 NAS 密码'), 'only-in-request')
  act(() => button('浏览共享').click())
  await until(() => !!button('部门'))
  act(() => button('部门').click())
  await until(() => !!button('选择当前目录') && !button('选择当前目录').disabled)
  act(() => button('选择当前目录').click())
  expect(field('共享内相对路径').value).toBe('部门')
  act(() => button('检查配置').click())
  await until(() => !button('保存配置').disabled)
  input(field('DSM HTTPS 端口'), '5002')
  expect(button('保存配置').disabled).toBe(true)
  act(() => button('检查配置').click())
  await until(() => !button('保存配置').disabled)
  act(() => button('保存配置').click())
  const dialog = document.querySelector('dialog')!
  expect(dialog.open).toBe(true)
  expect(calls.some(([url]) => url.endsWith('/apply'))).toBe(false)
  act(() => button('确认保存并待重启', dialog).click())
  await until(() => host.textContent?.includes('待重启') ?? false)
  expect(field('当前管理员 NAS 密码').value).toBe('')
  expect(calls.find(([url]) => url.endsWith('/apply'))?.[1]).toMatchObject({ confirmed: true, revision, work_root: '部门', actor_password: 'only-in-request' })
  expect(calls.find(([url]) => url.endsWith('/apply'))?.[1]).not.toHaveProperty('system_password')
})

it('关联资产阻断时显示原因且无法保存', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/validate') ?
    reply({ ...accepted, eligible: false, conflicts: ['已有正式资产，需要独立迁移'], checks: [] }) : reply(view)))
  mount()
  await until(() => !!button('检查配置'))
  input(field('共享内相对路径'), '其他工作台')
  input(field('当前管理员 NAS 密码'), 'only-in-request')
  act(() => button('检查配置').click())
  await until(() => host.textContent?.includes('已有正式资产') ?? false)
  expect(button('保存配置').disabled).toBe(true)
})
