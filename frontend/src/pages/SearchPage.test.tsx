// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { SearchPage } from './SearchPage'
import { VectorIndexPanel } from '../components/VectorIndexPanel'
import { GlassProvider } from '../components/glass/GlassProvider'
import { MODE_KEY } from '../components/glass/visualMode'
import { useAuthStore } from '../stores/auth'
import type { SearchResponse, SearchResult } from '../api/search'

let root: Root
let host: HTMLDivElement
let query: QueryClient
let posts: { url: string; body: Record<string, unknown> }[]
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const result = (id: number, name: string, extra: Partial<SearchResult> = {}): SearchResult => ({ id, product_code: `P2026-0012-00${id}`, display_name: name, kind: 'item',
  tags: [{ key: 'size', label: '尺寸', value: '掌心' }, { key: 'category', label: '品类', value: '摆件' }, { key: 'festival', label: '节日场景', value: '新年' }], free_text: '福字小物', related: [],
  project: { id: 3, project_code: 'P2026-0012', project_name: '中秋礼盒', company_name: '泸州老窖', won: true, link_id: 'p' }, link_id: `l${id}`, reason: '符合过年送礼', matched: ['节日场景'], by_keywords: true, by_vector: true, similarity: 0.8, ...extra })
const base: SearchResponse = { log_id: 7, query: '过年送领导的小摆件', kind: 'natural', relaxed: false, degraded: false, notes: [], from_cache: false, model_calls: 3, elapsed_ms: 420, local_ms: 30, total: 2, page: 1, page_size: 20,
  conditions: [{ type: 'field', field: 'festival', label: '节日场景', terms: ['过年', '春节', '新年'] }, { type: 'field', field: 'audience', label: '受众群体', terms: ['领导', '企业客户', '上级'] }, { type: 'exclude', field: null, label: '排除', terms: ['塑料'] }],
  results: [result(1, '福字摆件'), result(2, '茶具套装', { kind: 'set', related: [{ id: 1, product_code: 'P2026-0012-001', display_name: '福字摆件' }], reason: null, matched: [], by_keywords: false })],
  project_hits: [{ id: 3, project_code: 'P2026-0012', project_name: '中秋礼盒', company_name: '泸州老窖', won: true, link_id: 'p' }] }

function serve(reply: (body: Record<string, unknown>) => unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    if (options.method === 'POST') { const body = JSON.parse(String(options.body ?? '{}')) as Record<string, unknown>; posts.push({ url, body }); return response(url.endsWith('/click') ? { message: '已记录' } : reply(body)) }
    return response(url === '/api/catalog/tree' ? [{ won: true, folder: '01已中标项目', industries: [{ id: 1, name: '酒业', folder: '01酒业', companies: [{ id: 7, name: '泸州老窖', folder: '07泸州老窖' }] }] }] : {})
  }))
}
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 150; index++) { await settle(); if (condition()) return }
  throw new Error('界面没有完成预期更新')
}
function mount() {
  act(() => root.render(<QueryClientProvider client={query}><GlassProvider><MemoryRouter><SearchPage /></MemoryRouter></GlassProvider></QueryClientProvider>))
}
async function submit(text: string) {
  const input = host.querySelector<HTMLInputElement>('[aria-label="搜索内容"]')!
  act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, text); input.dispatchEvent(new Event('input', { bubbles: true })) })
  await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  localStorage.clear(); localStorage.setItem(MODE_KEY, 'lite')
  posts = []
  query = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  useAuthStore.getState().setUser({ id: 1, username: 'me', display_name: null, roles: ['uploader'], enabled: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); query.clear(); host.remove(); vi.unstubAllGlobals(); useAuthStore.getState().clear() })

it('显示系统理解的条件（含同义说法）、结果卡片与项目命中，点击结果会记录', async () => {
  serve(() => base); mount()
  await submit('过年送领导的小摆件')
  await until(() => !!host.querySelector('[data-testid="results"]'))
  const understood = host.querySelector('[data-testid="understood"]')!.textContent!
  expect(understood).toContain('节日场景：过年（也找：春节、新年）')
  expect(understood).toContain('受众群体：领导（也找：企业客户、上级）')
  expect(understood).toContain('排除：塑料')
  const card = host.querySelector('[data-result="P2026-0012-001"]')!
  expect(card.querySelector('img')!.getAttribute('src')).toBe('/api/thumbs/1/300')
  expect(card.textContent).toContain('福字摆件'); expect(card.textContent).toContain('符合过年送礼'); expect(card.textContent).toContain('P2026-0012 泸州老窖')
  // 关键标签优先显示品类。
  expect([...card.querySelectorAll('ul li')].map(item => item.textContent)).toEqual(['摆件', '新年', '掌心'])
  const second = host.querySelector('[data-result="P2026-0012-002"]')!
  expect(second.textContent).toContain('包含：福字摆件'); expect(second.textContent).toContain('语义相近')
  expect(host.querySelector('[data-testid="project-hits"] a')!.getAttribute('href')).toBe('/projects/3')
  expect(host.textContent).toContain('共 2 个候选 · 用时 420 毫秒 · 模型调用 3 次')
  expect(posts[0]).toEqual({ url: '/api/search', body: { query: '过年送领导的小摆件', filters: { won: null, industry: null, company: null }, page: 1 } })
  act(() => card.querySelector('a')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })))
  await until(() => posts.some(item => item.url.endsWith('/click')))
  expect(posts.find(item => item.url.endsWith('/click'))!.body).toEqual({ log_id: 7, product_id: 1 })
})

it('删除一个条件后带着剩下的条件重新搜索，结果随之变化', async () => {
  serve(body => body.conditions ? { ...base, log_id: 8, conditions: body.conditions, results: [base.results[0]], total: 1 } : base); mount()
  await submit('过年送领导的小摆件')
  await until(() => host.querySelectorAll('[data-result]').length === 2)
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="去掉条件 节日场景：过年"]')!.click())
  await until(() => host.querySelectorAll('[data-result]').length === 1)
  expect(posts[1].body.conditions).toEqual(base.conditions.slice(1))
  expect(host.querySelector('[data-testid="understood"]')!.textContent).not.toContain('节日场景')
})

it('空结果与降级时给出提示；编号查询说明未调用模型；筛选器随请求发送', async () => {
  let reply: SearchResponse = { ...base, results: [], total: 0, project_hits: [], degraded: true, notes: ['排序模型暂不可用，结果按关键词命中和相似度排列'] }
  serve(() => reply); mount()
  await until(() => host.querySelectorAll('[aria-label="行业"] option').length === 2)
  act(() => { const select = host.querySelector<HTMLSelectElement>('[aria-label="中标状态"]')!; select.value = 'true'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  act(() => { const select = host.querySelector<HTMLSelectElement>('[aria-label="企业"]')!; select.value = '7'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await submit('不存在的东西')
  await until(() => !!host.querySelector('[data-testid="empty"]'))
  expect(host.querySelector('[data-testid="degraded"]')!.textContent).toContain('本次搜索已降级：排序模型暂不可用，结果按关键词命中和相似度排列')
  expect(host.querySelector('[data-testid="empty"]')!.textContent).toContain('没有找到相关的商品')
  expect(posts[0].body.filters).toEqual({ won: true, industry: null, company: 7 })
  reply = { ...base, kind: 'code', query: 'P2026-0012-001', conditions: [], model_calls: 0, results: [base.results[0]], total: 1 }
  await submit('P2026-0012-001')
  await until(() => host.querySelector('[data-testid="understood"]')!.textContent === '按编号「P2026-0012-001」精确查找，没有调用模型。')
  expect(host.querySelector('[data-testid="degraded"]')).toBeNull()
})

it('管理端可以查看向量索引状态并重建全部向量', async () => {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => { calls.push(`${options.method ?? 'GET'} ${url}`)
    return response(options.method === 'POST' ? { backend: 'sqlite-vec', model: 'embed-v3', products: 120, vectors: 0, dim: 1024, queued: 120 } : { backend: 'sqlite-vec', model: 'embed-v3', products: 120, vectors: 118, dim: 1024, queued: 0 }) }))
  act(() => root.render(<QueryClientProvider client={query}><GlassProvider><VectorIndexPanel /></GlassProvider></QueryClientProvider>))
  await until(() => !!host.querySelector('[data-testid="index-status"]'))
  expect(host.querySelector('[data-testid="index-status"]')!.textContent).toBe('已有向量 118 / 有效商品 120 · 实现 sqlite-vec · 模型 embed-v3（1024 维）')
  act(() => [...host.querySelectorAll('button')].find(item => item.textContent === '重建全部向量')!.click())
  await until(() => host.querySelector('[data-testid="index-status"]')!.textContent!.includes('还有 120 个在排队'))
  expect(calls).toContain('POST /api/admin/search-index/rebuild')
})
