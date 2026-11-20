// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { VectorIndexPanel } from '../components/VectorIndexPanel'
import { SearchPage } from './SearchPage'

let root: Root
let host: HTMLDivElement
let cache: QueryClient
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  act(() => root.unmount())
  cache.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function mount(component: ReactNode) {
  act(() => root.render(<MemoryRouter><QueryClientProvider client={cache}>{component}</QueryClientProvider></MemoryRouter>))
}
function set(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const prototype = element instanceof HTMLInputElement ? HTMLInputElement.prototype :
    element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLSelectElement.prototype
  act(() => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }))
  })
}
async function until(predicate: () => boolean) {
  for (let i = 0; i < 80; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    if (predicate()) return
  }
  throw new Error('界面未显示预期结果')
}
const base = { search_id: 8, mode: 'semantic', projects: [], products: [], conditions: { fields: {}, keywords: [], exclude: [] },
  warnings: [], degraded: false, exact: false, permission_filtered: false, field_labels: { material: '材质' },
  index_generation: 12 }

it('发送筛选条件、显示降级并在删除解释条件后重新检索', async () => {
  const bodies: unknown[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit) => {
    if (url === '/api/catalog/tree') return new Response(JSON.stringify([{ industries: [{ id: 2, name: '酒业', companies: [] }] }]))
    bodies.push(JSON.parse(String(options.body)))
    return new Response(JSON.stringify(bodies.length === 1 ? { ...base,
      conditions: { fields: { material: ['陶瓷', '瓷器'] }, keywords: [], exclude: [] },
      degraded: true, warnings: ['正式向量模型暂不可用，已使用关键词检索'], permission_filtered: true } : base))
  }))
  mount(<SearchPage />)
  await until(() => host.textContent!.includes('酒业'))
  set(host.querySelector('select[aria-label=行业]')!, '2')
  set(host.querySelector('select[aria-label=中标状态]')!, 'true')
  act(() => host.querySelectorAll<HTMLButtonElement>('[aria-label="检索模式"] button')[1].click())
  set(host.querySelector('textarea')!, '瓷器')
  act(() => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  await until(() => host.textContent!.includes('已使用关键词检索'))
  expect(host.textContent).toContain('因 NAS 权限')
  expect(host.textContent).toContain('AI 搜索部分能力不可用')
  expect(host.textContent).toContain('没有找到匹配商品')
  expect(bodies[0]).toEqual({ mode: 'semantic', query: '瓷器', won: true, industry: '2', company: '', remove: [] })
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="移除 材质 · 陶瓷、瓷器"]')!.click())
  await until(() => bodies.length === 2 && !host.textContent!.includes('已使用关键词检索'))
  expect(bodies[1]).toEqual({ mode: 'semantic', query: '瓷器', won: true, industry: '2', company: '', remove: ['material'] })
})

it('商品卡片链接到 WP10，搜索错误有明确提示', async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === '/api/catalog/tree') return new Response('[]')
    if (fetcher.mock.calls.filter(([path]) => path === '/api/search').length === 2)
      return new Response(JSON.stringify({ message: '搜索暂不可用' }), { status: 503 })
    return new Response(JSON.stringify({ ...base, mode: 'quick', index_generation: null, products: [{ id: 41, project_id: 8, project_code: 'P001', project_name: '礼盒',
      company_name: '酒企', won: true, product_code: 'P001-001', name: '青瓷盏', kind: 'item', tags: { material: '陶瓷' },
      related: ['茶具套装'], matches: ['material'], reason: '陶瓷材质匹配', url: 'http://nas.example/l/item' }] }))
  })
  vi.stubGlobal('fetch', fetcher)
  mount(<SearchPage />)
  set(host.querySelector('input[type=search]')!, '青瓷')
  const submit = () => act(() => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  submit()
  await until(() => host.textContent!.includes('青瓷盏'))
  expect(host.querySelector('a[href="/products/41"]')).not.toBeNull()
  expect(host.querySelector('a[href="http://nas.example/l/item"]')).not.toBeNull()
  submit()
  await until(() => host.textContent!.includes('搜索暂不可用'))
})

it('设置页候选覆盖不足时禁用切换，覆盖完整后可以提交', async () => {
  let operations = 0
  const row = { generation: 3, status: 'candidate', name: '升级模型', revision: '2026', version: 'sha', dimension: 64,
    pending: 1, ready: 1, total: 2, verified: false, failures: 0, running: false }
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url.endsWith('/retry')) {
      operations++
      return new Response(JSON.stringify({ backend: 'numpy', indexes: [{ ...row, verified: true, ready: 2, pending: 0 }] }))
    }
    if (url.endsWith('/switch')) {
      operations++
      return new Response(JSON.stringify({ backend: 'numpy', indexes: [{ ...row, status: 'formal', verified: true, ready: 2, pending: 0 }] }))
    }
    return new Response(JSON.stringify({ backend: 'numpy', indexes: [row] }))
  }))
  mount(<VectorIndexPanel />)
  const button = (label: string) => [...host.querySelectorAll('button')].find(button => button.textContent === label)
  await until(() => !!button('切换为正式'))
  expect(button('切换为正式')?.disabled).toBe(true)
  act(() => button('补齐索引')!.click())
  await until(() => !button('切换为正式')?.disabled)
  act(() => button('切换为正式')!.click())
  await until(() => host.textContent!.includes('正式 · 第 3 代'))
  expect(operations).toBe(2)
})

it('切换模式保留输入与三个筛选、清理旧请求，当前结果共用商品和链接', async () => {
  type Mode = 'quick' | 'semantic'
  type Request = { body: { mode: Mode; query: string; industry: string; company: string; won: boolean }; respond: (r: Response) => void; signal: AbortSignal }
  const calls: Request[] = []
  const fetcher = vi.fn(async (url: string, options: RequestInit) => {
    if (url === '/api/catalog/tree') return new Response(JSON.stringify([{ industries: [{ id: 2, name: '酒业', companies: [{ id: 9, name: '酒企' }] }] }]))
    if (url === '/api/search') return new Promise<Response>(respond => calls.push({ body: JSON.parse(String(options.body)), respond, signal: options.signal! }))
    if (url === '/api/search/click') return new Response(null, { status: 204 })
    return new Response(JSON.stringify({ message: '模拟商品详情' }), { status: 404 })
  })
  vi.stubGlobal('fetch', fetcher)
  const product = (name: string, reason: string) => ({ id: 41, project_id: 8, project_code: 'P001', project_name: '礼盒项目',
    company_name: '酒企', won: true, product_code: 'P001-001', name, kind: 'item', tags: { material: '陶瓷' },
    related: [], matches: ['material'], reason, url: 'http://nas.example/l/item' })
  const resolve = (i: number, mode: Mode, name: string, reason: string, error = false) => act(() => calls[i].respond(error
    ? new Response(JSON.stringify({ message: reason }), { status: 503 }) : new Response(JSON.stringify({
      ...base, mode, index_generation: mode === 'quick' ? null : 12, products: [product(name, reason)],
    }))))
  const buttons = () => host.querySelectorAll<HTMLButtonElement>('[aria-label="检索模式"] button')
  const selectMode = (i: number) => act(() => buttons()[i].click())
  const submit = () => act(() => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))

  mount(<SearchPage />)
  expect(buttons()[0].getAttribute('aria-pressed')).toBe('true')
  const input = host.querySelector<HTMLInputElement>('input[type=search]')!
  expect(input.placeholder).toContain('陶瓷茶具')
  await until(() => host.textContent!.includes('酒业'))
  set(host.querySelector('select[aria-label=行业]')!, '2')
  set(host.querySelector('select[aria-label=企业]')!, '9')
  set(host.querySelector('select[aria-label=中标状态]')!, 'true')
  set(input, '陶瓷茶具')
  submit()
  expect(calls[0].body).toMatchObject({ mode: 'quick', query: '陶瓷茶具', industry: '2', company: '9', won: true })
  selectMode(1)
  expect(calls).toHaveLength(1)
  expect(calls[0].signal.aborted).toBe(true)
  const area = host.querySelector('textarea')!
  expect(area.value).toBe('陶瓷茶具')
  expect(area.placeholder).toContain('高净值客户')
  submit()
  expect(calls[1].body).toEqual({ ...calls[0].body, mode: 'semantic' })
  resolve(1, 'semantic', '商务茶具', '符合商务赠礼需求')
  await until(() => host.textContent!.includes('符合商务赠礼需求'))
  resolve(0, 'quick', '旧请求结果', '旧的匹配理由')
  expect(host.textContent).not.toContain('旧请求结果')
  expect(host.querySelector('a[href="/projects/8"]')).not.toBeNull()
  expect(host.querySelector('a[href="http://nas.example/l/item"]')).not.toBeNull()
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="查看 商务茶具 的匹配详情"]')!.click())
  expect(host.textContent).toContain('匹配详情')
  selectMode(0)
  expect(host.textContent).not.toContain('商务茶具')
  expect(host.textContent).not.toContain('匹配详情')
  expect(calls).toHaveLength(2)
  submit()
  submit()
  resolve(3, 'quick', '本地茶杯', '名称匹配：茶杯')
  resolve(2, 'quick', '', '早先失败', true)
  await until(() => host.textContent!.includes('本地茶杯'))
  expect(host.textContent).not.toContain('早先失败')
  expect(host.querySelector('a[href="/products/41"]')).not.toBeNull()
  expect(host.querySelector('a[href="/projects/8"]')).not.toBeNull()
  expect(host.querySelector('a[href="http://nas.example/l/item"]')).not.toBeNull()
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="查看 本地茶杯 的匹配详情"]')!.click())
  expect(host.textContent).toContain('匹配详情')
})
