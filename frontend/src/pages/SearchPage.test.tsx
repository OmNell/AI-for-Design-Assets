// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { GlassProvider } from '../components/glass/GlassProvider'
import { VectorIndexPanel } from '../components/VectorIndexPanel'
import { SearchPage } from './SearchPage'

let root: Root
let host: HTMLDivElement
let cache: QueryClient
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
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
  act(() => root.render(<MemoryRouter><QueryClientProvider client={cache}><GlassProvider>{component}</GlassProvider></QueryClientProvider></MemoryRouter>))
}
function set(element: HTMLInputElement | HTMLSelectElement, value: string) {
  const prototype = element instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLSelectElement.prototype
  act(() => {
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event(element instanceof HTMLInputElement ? 'input' : 'change', { bubbles: true }))
  })
}
async function until(predicate: () => boolean) {
  for (let i = 0; i < 80; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    if (predicate()) return
  }
  throw new Error('界面未显示预期结果')
}
const base = { search_id: 8, projects: [], products: [], conditions: { fields: {}, keywords: [], exclude: [] },
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
  set(host.querySelector('input[type=search]')!, '瓷器')
  act(() => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
  await until(() => host.textContent!.includes('已使用关键词检索'))
  expect(host.textContent).toContain('因 NAS 权限')
  expect(host.textContent).toContain('没有找到匹配商品')
  expect(bodies[0]).toEqual({ query: '瓷器', won: true, industry: '2', company: '', remove: [] })
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="移除 材质 · 陶瓷、瓷器"]')!.click())
  await until(() => bodies.length === 2 && !host.textContent!.includes('已使用关键词检索'))
  expect(bodies[1]).toEqual({ query: '瓷器', won: true, industry: '2', company: '', remove: ['material'] })
})

it('商品卡片链接到 WP10，搜索错误有明确提示', async () => {
  const fetcher = vi.fn(async (url: string) => {
    if (url === '/api/catalog/tree') return new Response('[]')
    if (fetcher.mock.calls.filter(([path]) => path === '/api/search').length === 2)
      return new Response(JSON.stringify({ message: '搜索暂不可用' }), { status: 503 })
    return new Response(JSON.stringify({ ...base, products: [{ id: 41, project_id: 8, project_code: 'P001', project_name: '礼盒',
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
