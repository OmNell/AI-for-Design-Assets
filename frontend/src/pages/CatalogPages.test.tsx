// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AssetLinkPage } from './AssetLinkPage'
import { ProductPage } from './ProductPage'
import { ProjectPage } from './ProjectPage'
import { ProjectsPage } from './ProjectsPage'
import { sequentialDownload } from '../lib/download'

let host: HTMLElement
let root: Root
let query: QueryClient
const domain = 'http://assets.example.local:8080'
const project = { id: 42, project_code: 'P001', project_name: '节日礼盒', company_name: '企业', industry_name: '酒业',
  won: true, status: 'active', ingested_at: '2026-11-18T12:00:00Z', link_url: domain + '/l/project', products: 1, files: 2 }
const brief = { id: 55, kind: 'set', product_code: 'P001-001', display_name: '红色套装', tags: ['丝绸'], status: 'active', related: [] }
const file = { id: 72, name: '中文设计.pdf', rel_path: '01定稿文件/中文设计.pdf', size: 1024, mtime: 1720000000000, status: 'active', link_url: domain + '/l/file', is_image: false }

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  query.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function mockFetch(fn: (url: string) => unknown) {
  const fetcher = vi.fn(async (url: string) => {
    const result = fn(url)
    return result instanceof Response ? result : new Response(JSON.stringify(result))
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}
function mount(path: string, view: ReactNode) {
  act(() => root.render(<QueryClientProvider client={query}><MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/l/:linkId" element={view} />
      <Route path="/projects" element={view} />
      <Route path="/projects/:id" element={path.startsWith('/l/') ? <p>已打开项目详情</p> : view} />
      <Route path="/products/:id" element={path.startsWith('/l/') ? <p>已打开商品详情</p> : view} />
    </Routes>
  </MemoryRouter></QueryClientProvider>))
}
async function seen(text: string) {
  for (let i = 0; i < 80; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    if (host.textContent?.includes(text)) return
  }
  throw new Error(`缺少界面文字：${text}`)
}

it('项目列表显示永久链接，支持翻页与排序', async () => {
  const fetcher = mockFetch(url => url === '/api/catalog/tree' ? [] : { items: [project], total: 22, page: 1, page_size: 20 })
  mount('/projects', <ProjectsPage />)
  await seen('节日礼盒')
  expect(host.querySelector(`a[href="${project.link_url}"]`)).not.toBeNull()
  const sorting = host.querySelector<HTMLSelectElement>('select[aria-label="排序"]')!
  act(() => { sorting.value = 'code'; sorting.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(fetcher.mock.calls.some(([url]) => url.includes('sort=code'))).toBe(true)
})

it('项目页能进入资料夹、查看文件详情、选择文件并切换商品和历史', async () => {
  const folders = ['01定稿文件', '02设计方案', '03拍摄照片及视频', '04授权文件', '05 详情页']
  const nested = { ...file, id: 73, name: '印刷说明.ai', rel_path: '01定稿文件/设计交付/印刷说明.ai' }
  const mock = mockFetch(url => url === '/api/catalog/tree' ? [] : url === '/api/projects/42/files' ? [file, nested] :
    url === '/api/files/72/check' ? { ...file, warning: null, url: '/api/files/72/download?transfer=abcdefghijkl' } :
    { project, products_list: [brief], folders: folders.map((name, i) => ({ name, count: i ? 0 : 2 })), history: [], logs: [] })
  mount('/projects/42', <ProjectPage />)
  const button = (label: string) => [...host.querySelectorAll('button')].find(button => button.textContent?.trim() === label)!
  await seen('01定稿文件')
  act(() => button('01定稿文件').click())
  await seen('中文设计.pdf')
  act(() => button('中文设计.pdf').click())
  await seen('下载文件')
  expect(mock.mock.calls.some(([url]) => url === '/api/files/72/check')).toBe(true)
  expect(button('复制永久链接')).toBeDefined()
  act(() => host.querySelector<HTMLInputElement>('input[aria-label="选择 中文设计.pdf"]')!.click())
  expect(button('下载所选文件（1）').disabled).toBe(false)
  act(() => host.querySelector<HTMLButtonElement>('button[aria-label="缩略图视图"]')!.click())
  expect(host.querySelector('button[aria-label="缩略图视图"]')?.getAttribute('aria-pressed')).toBe('true')
  act(() => button('设计交付').click())
  await seen('印刷说明.ai')
  act(() => button('项目根目录').click())
  act(() => button('05 详情页').click())
  await seen('此资料夹暂无文件')
  expect(host.textContent).not.toContain('中文设计.pdf')
  act(() => button('商品资产').click())
  await seen('红色套装')
  act(() => button('历史版本').click())
  await seen('暂无历史版本')
})

it('套装卡片能直接展开其单品和复制永久链接', async () => {
  const item = { ...brief, id: 56, kind: 'item', display_name: '礼盒内酒杯', related: [55] }
  const setLink = domain + '/l/set'
  const copy = vi.fn(async () => undefined)
  const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } })
  mockFetch(url => url === '/api/projects/42'
    ? { project, products_list: [{ ...brief, related: [56], link_url: setLink }, item], folders: [], history: [], logs: [] }
    : [])
  mount('/projects/42', <ProjectPage />)
  await seen('商品资产')
  act(() => [...host.querySelectorAll('button')].find(node => node.textContent === '商品资产')!.click())
  await seen('包含的单品（1）')
  const detail = host.querySelector('details')!
  expect(detail.open).toBe(false)
  act(() => detail.querySelector('summary')!.click())
  expect(detail.open).toBe(true)
  expect(detail.querySelector('a')?.getAttribute('href')).toBe('/products/56')
  const link = host.querySelector<HTMLInputElement>('input[aria-label="复制商品链接地址"]')
  expect(link?.value).toBe(setLink)
  try {
    act(() => [...host.querySelectorAll('button')].find(node => node.textContent === '复制商品链接')!.click())
    await seen('已复制')
    expect(copy).toHaveBeenCalledWith(setLink)
  } finally {
    if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
})

it('目录按中标、行业和企业筛选，并使用已有项目读取接口定位', async () => {
  const mock = mockFetch(url => url === '/api/catalog/tree' ? [{ won: true, folder: '01已中标项目', industries: [
    { id: 8, name: '酒业', companies: [{ id: 15, name: '企业' }] },
  ] }] : { items: [project], total: 1, page: 1, page_size: 20 })
  mount('/projects', <ProjectsPage />)
  await seen('已中标')
  const branch = (name: string) => [...host.querySelectorAll<HTMLButtonElement>('nav[aria-label="资产目录"] button')].find(node => node.textContent === name)!
  act(() => branch('已中标').click())
  await seen('酒业')
  act(() => branch('酒业').click())
  await seen('企业')
  act(() => branch('企业').click())
  expect(mock.mock.calls.some(([url]) => url.includes('won=true') && url.includes('industry=8') && url.includes('company=15'))).toBe(true)
  await seen('节日礼盒')
  expect(host.querySelector('nav[aria-label="资产目录"] a[href="/projects/42"]')).not.toBeNull()
})

it('商品详情使用人工确认状态展示空标签', async () => {
  mockFetch(url => url.startsWith('/api/files') ? new Response(new Blob(['JPEG']), { headers: { 'Content-Type': 'image/jpeg' } }) :
    { ...brief, file_id: 72, product_url: domain + '/l/product', project, related: [],
      tags: [{ key: 'festival', label: '节日场景', value: '', confirmation_status: 'confirmed_unknown' }], free_text: '适合摆放' })
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL: () => 'blob:test', revokeObjectURL: () => undefined }))
  mount('/products/55', <ProductPage />)
  await seen('已确认无法判断')
  expect(host.textContent).toContain('适合摆放')
})

for (const [kind, caption] of [['project', '已打开项目详情'], ['product', '已打开商品详情']] as const) {
  it(`永久链接导航到${caption}`, async () => {
    mockFetch(() => ({ kind, target_id: 42, status: 'active', url: domain + '/l/id' }))
    mount('/l/id', <AssetLinkPage />)
    await seen(caption)
  })
}

it('文件链接展示作废警告和授权下载入口', async () => {
  mockFetch(url => url.startsWith('/api/l/') ? ({ kind: 'file', target_id: file.id, status: 'voided', url: domain + '/l/id' }) :
    { ...file, warning: null, status: 'voided', url: '/api/files/72/download?transfer=abcdefghijkl' })
  mount('/l/id', <AssetLinkPage />)
  await seen('该资产已作废')
  expect(host.textContent).toContain('下载文件')
})

it('已物理删除的链接只展示状态', async () => {
  const fetcher = mockFetch(() => ({ kind: 'file', target_id: 72, status: 'deleted' }))
  mount('/l/id', <AssetLinkPage />)
  await seen('该资产已物理删除')
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(host.textContent).not.toContain('下载文件')
})

it('批量下载按后端发送状态逐个处理，保留独立失败原因', async () => {
  const marks: string[] = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { marks.push(this.pathname) })
  mockFetch(url => {
    if (url === '/api/files/download-many') return [
      { file_id: 1, name: '一', url: '/api/files/1/download?transfer=abcdefghijkl' },
      { file_id: 2, name: '二', error: 'NAS 无权限' },
      { file_id: 3, name: '三', url: '/api/files/3/download?transfer=abcdefghijkl' },
    ]
    marks.push(url)
    return { status: 'sent' }
  })
  const report = vi.fn()
  await sequentialDownload([1, 2, 3], report)
  expect(marks).toEqual(['/api/files/1/download', '/api/files/1/downloads/abcdefghijkl',
    '/api/files/3/download', '/api/files/3/downloads/abcdefghijkl'])
  expect(report).toHaveBeenLastCalledWith(expect.objectContaining({ total: 3, sent: 2, failed: [{ name: '二', message: 'NAS 无权限' }] }))
})
