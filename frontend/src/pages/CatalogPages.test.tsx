// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ProjectsPage } from './ProjectsPage'
import { ProjectPage } from './ProjectPage'
import { ProductPage } from './ProductPage'
import { LinkPage } from './LinkPage'
import { GlassProvider } from '../components/glass/GlassProvider'
import { MODE_KEY } from '../components/glass/visualMode'
import { useAuthStore } from '../stores/auth'
import { downloadMessage } from '../lib/download'
import { ApiError } from '../api/client'
import type { ProjectDetail, ProjectRow } from '../api/catalog'

let root: Root
let host: HTMLDivElement
let query: QueryClient
let requests: string[]
let downloads: string[]
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const row: ProjectRow = { id: 3, project_code: 'P2026-0012', project_name: '中秋礼盒', company_name: '泸州老窖', industry_name: '酒业', won: true,
  current_path: '01已中标项目/01酒业/07泸州老窖/P2026-0012_泸州老窖_中秋礼盒', status: 'active', ingested_at: '2026-11-10T02:00:00Z', products: 3, files: 6 }
const detail: ProjectDetail = { project: row, folder_name: 'P2026-0012_泸州老窖_中秋礼盒', code_source: 'company', link_id: 'PRJLINK001',
  products_list: [{ id: 1, product_code: 'P2026-0012-001', display_name: '酒瓶', kind: 'item', status: 'active', link_id: 'a', related: [3] },
    { id: 2, product_code: 'P2026-0012-002', display_name: '月饼盒', kind: 'item', status: 'active', link_id: 'b', related: [3] },
    { id: 3, product_code: 'P2026-0012-003', display_name: '中秋礼盒A', kind: 'set', status: 'active', link_id: 'c', related: [1, 2] }],
  folders: ['01定稿文件', '02设计方案', '03拍摄照片及视频', '04授权文件', '05 详情页'].map((name, index) => ({ name, count: index === 0 ? 5 : 0 })),
  history: [], logs: [{ ts: '2026-11-10T02:00:00Z', user: '王五', action: '入库完成', result: '成功' }] }
const files = Array.from({ length: 5 }, (_, index) => ({ id: 10 + index, name: `文件${index + 1}.ai`, rel_path: `01定稿文件/文件${index + 1}.ai`, size: 1024 * (index + 1), mtime: 1700000000000, status: 'active', category: '01定稿文件', link_id: `f${index}` }))

function serve(routes: (url: string) => unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    requests.push(`${options.method ?? 'GET'} ${url}`)
    const result = routes(url)
    return result instanceof Response ? result : response(result)
  }))
}
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  // 批量下载每个文件之间有意停顿，等待时间要覆盖 5 个文件。
  for (let index = 0; index < 700; index++) { await settle(); if (condition()) return }
  throw new Error('界面没有完成预期更新')
}
function mount(node: ReactNode, route: string) {
  act(() => root.render(<QueryClientProvider client={query}><GlassProvider><MemoryRouter initialEntries={[route]}>{node}</MemoryRouter></GlassProvider></QueryClientProvider>))
}
const button = (label: string, scope: ParentNode = document) => [...scope.querySelectorAll('button')].find(value => value.textContent === label)!

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  localStorage.clear(); localStorage.setItem(MODE_KEY, 'lite')
  requests = []; downloads = []
  // 浏览器下载由隐藏的链接触发；测试里记录被点击的地址。
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.getAttribute('href') ?? '') })
  query = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  useAuthStore.getState().setUser({ id: 1, username: 'me', display_name: null, roles: ['uploader'], enabled: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); query.clear(); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); useAuthStore.getState().clear() })

it('项目列表可按关键字和中标状态查找，并链接到项目页', async () => {
  serve(url => url === '/api/catalog/tree' ? [{ won: true, folder: '01已中标项目', industries: [{ id: 1, name: '酒业', folder: '01酒业', companies: [{ id: 7, name: '泸州老窖', folder: '07泸州老窖' }] }] }] : { items: [row], total: 1, page: 1, page_size: 20 })
  mount(<ProjectsPage />, '/projects')
  await until(() => host.querySelectorAll('tbody tr').length === 1)
  expect(host.querySelector('tbody tr')!.textContent).toContain('P2026-0012中秋礼盒泸州老窖酒业是36')
  expect(host.querySelector('a[href="/projects/3"]')).not.toBeNull()
  const input = host.querySelector<HTMLInputElement>('[aria-label="搜索项目"]')!
  act(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '泸州'); input.dispatchEvent(new Event('input', { bubbles: true })) })
  act(() => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
  await until(() => requests.some(item => item.includes('q=%E6%B3%B8%E5%B7%9E')))
  act(() => { const select = host.querySelector<HTMLSelectElement>('[aria-label="中标状态"]')!; select.value = 'false'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  await until(() => requests.some(item => item.includes('won=false') && item.includes('page=1')))
  expect(host.textContent).toContain('共 1 个项目')
})

it('项目页显示位置、商品网格（套装可展开）、资料夹文件、操作记录，入库后操作为禁用占位', async () => {
  serve(url => url.includes('/files?') ? files : detail)
  mount(<Routes><Route path="/projects/:id" element={<ProjectPage />} /></Routes>, '/projects/3')
  await until(() => host.querySelectorAll('tbody input[type="checkbox"]').length === 5)
  expect(host.textContent).toContain('位置 01已中标项目 / 01酒业 / 07泸州老窖')
  expect(host.textContent).toContain('商品 3 个')
  expect(host.querySelector<HTMLImageElement>('[data-product="P2026-0012-001"] img')!.getAttribute('src')).toBe('/api/thumbs/1/300')
  const box = host.querySelector('[data-product="P2026-0012-003"]')!
  act(() => button('包含 2 个单品', box).click())
  expect([...box.querySelectorAll('li')].map(item => item.textContent)).toEqual(['P2026-0012-001 酒瓶', 'P2026-0012-002 月饼盒'])
  expect([...host.querySelectorAll('[role="tab"]')].map(tab => tab.textContent)).toEqual(['01定稿文件（5）', '02设计方案（0）', '03拍摄照片及视频（0）', '04授权文件（0）', '05 详情页（0）'])
  const operations = [...host.querySelectorAll<HTMLButtonElement>('[aria-label="入库后操作"] button')]
  expect(operations.map(item => item.textContent)).toEqual(['补充', '改名', '修改标签', '作废'])
  expect(operations.every(item => item.disabled)).toBe(true)
  expect(host.querySelector('[data-testid="logs"]')!.textContent).toContain('王五入库完成成功')
  expect(requests).toContain(`GET /api/projects/3/files?category=${encodeURIComponent('01定稿文件')}`)
})

it('多选 5 个文件依次下载并显示进度；单个下载先探测再交给浏览器', async () => {
  serve(url => url.endsWith('/download-many') ? files.map(file => ({ file_id: file.id, name: file.name, size: file.size, url: `/api/files/${file.id}/download` })) :
    url.endsWith('/check') ? { id: 1, name: 'x', size: 1 } : url.includes('/files?') ? files : detail)
  mount(<Routes><Route path="/projects/:id" element={<ProjectPage />} /></Routes>, '/projects/3')
  await until(() => !!host.querySelector('[aria-label="全选"]'))
  expect(button('下载所选（0）').disabled).toBe(true)
  act(() => host.querySelector<HTMLInputElement>('[aria-label="全选"]')!.click())
  act(() => button('下载所选（5）').click())
  await until(() => !!host.querySelector('[data-testid="download-progress"]')?.textContent?.includes('正在下载'))
  await until(() => host.querySelector('[data-testid="download-progress"]')!.textContent === '已依次触发 5 个下载')
  expect(downloads).toEqual([10, 11, 12, 13, 14].map(id => `/api/files/${id}/download`))
  expect(requests.filter(item => item.endsWith('/check'))).toHaveLength(5)
  act(() => [...host.querySelectorAll('tbody button')].find(item => item.textContent === '下载')!.dispatchEvent(new MouseEvent('click', { bubbles: true })))
  await until(() => downloads.length === 6)
  expect(downloads[5]).toBe('/api/files/10/download')
})

it('下载失败时分别提示 NAS 无权限、文件已不存在、需重新登录', async () => {
  const failures: Record<string, Response> = { '10': response({ code: 'nas_permission_denied', message: 'NAS 无权限' }, 403), '11': response({ code: 'file_missing', message: '文件已不存在' }, 404) }
  serve(url => url.endsWith('/download-many') ? files.slice(0, 3).map(file => ({ file_id: file.id, name: file.name, size: file.size, url: `/api/files/${file.id}/download` })) :
    url.endsWith('/check') ? failures[url.split('/')[3]]?.clone() ?? { id: 12, name: 'x', size: 1 } : url.includes('/files?') ? files.slice(0, 3) : detail)
  mount(<Routes><Route path="/projects/:id" element={<ProjectPage />} /></Routes>, '/projects/3')
  await until(() => !!host.querySelector('[aria-label="全选"]'))
  act(() => host.querySelector<HTMLInputElement>('[aria-label="全选"]')!.click())
  act(() => button('下载所选（3）').click())
  await until(() => host.querySelector('[data-testid="download-progress"]')?.textContent === '已依次触发 1 个下载，2 个失败')
  const alerts = [...host.querySelectorAll('[role="alert"] li')].map(item => item.textContent)
  expect(alerts[0]).toBe('文件1.ai：NAS 无权限：你的 NAS 账号不能读取这个文件，请联系管理员在 NAS 上授权。')
  expect(alerts[1]).toBe('文件2.ai：文件已不存在：在 NAS 上找不到这个文件，已记入待巡检。')
  expect(downloads).toEqual(['/api/files/12/download'])
  expect(downloadMessage(new ApiError(401, 'nas_session_expired', 'x'))).toBe('NAS 连接已超时，请重新登录后再下载。')
  expect(downloadMessage(new ApiError(503, 'nas_unavailable', 'x'))).toBe('NAS 暂时连不上，请稍后重试。')
})

it('商品详情页显示大图、标签、关系、所属项目，并可复制商品链接和项目链接', async () => {
  const copied: string[] = []
  vi.stubGlobal('navigator', { ...navigator, userAgent: navigator.userAgent, clipboard: { writeText: async (value: string) => { copied.push(value) } } })
  serve(() => ({ id: 3, product_code: 'P2026-0012-003', display_name: '中秋礼盒A', kind: 'set', status: 'active', free_text: '红色礼盒', file_id: 9, image_rel_path: '套装/中秋礼盒A.jpg',
    tags: [{ key: 'category', label: '品类', value: '茶具套装', terms: ['茶具套装'], source: 'ai' }, { key: 'material', label: '材质', value: '陶瓷、木', terms: ['陶瓷', '木'], source: 'manual' }],
    related: detail.products_list.slice(0, 2), project: row, product_link: 'PRODLINK01', project_link: 'PRJLINK001', ingested_at: '2026-11-10T02:00:00Z' }))
  mount(<Routes><Route path="/products/:id" element={<ProductPage />} /></Routes>, '/products/3')
  await until(() => !!host.querySelector('[data-testid="tags"]'))
  expect(host.querySelector('img')!.getAttribute('src')).toBe('/api/thumbs/3/1600')
  expect(host.querySelector('[data-testid="tags"]')!.textContent).toBe('品类茶具套装AI材质陶瓷木人工')
  expect(host.textContent).toContain('包含的单品')
  expect([...host.querySelectorAll('[data-testid="related"] a')].map(link => link.getAttribute('href'))).toEqual(['/products/1', '/products/2'])
  expect(host.querySelector('a[href="/projects/3"]')!.textContent).toBe('P2026-0012 泸州老窖 中秋礼盒')
  await act(async () => { button('复制商品链接').click() })
  await act(async () => { button('复制项目链接').click() })
  expect(copied).toEqual([`${window.location.origin}/l/PRODLINK01`, `${window.location.origin}/l/PRJLINK001`])
  expect(button('已复制')).toBeDefined()
})

it('永久链接：商品和项目跳到对应页面，文件触发下载，已作废的目标显示提示', async () => {
  const landing = (target: unknown) => {
    serve(url => url.endsWith('/check') ? { id: 9, name: '终稿.ai', size: 1 } : target)
    mount(<Routes><Route path="/l/:linkId" element={<LinkPage />} /><Route path="/products/:id" element={<p>商品页</p>} /><Route path="/projects/:id" element={<p>项目页</p>} /></Routes>, '/l/abc')
  }
  landing({ link_id: 'abc', kind: 'product', target_id: 3, status: 'active', url: '/products/3', message: null })
  await until(() => host.textContent === '商品页')
  act(() => root.unmount()); query.clear(); root = createRoot(host)
  landing({ link_id: 'abc', kind: 'project', target_id: 3, status: 'active', url: '/projects/3', message: null })
  await until(() => host.textContent === '项目页')
  act(() => root.unmount()); query.clear(); root = createRoot(host)
  landing({ link_id: 'abc', kind: 'file', target_id: 9, status: 'active', url: '/api/files/9/download', message: null })
  await until(() => host.textContent!.includes('已开始下载：终稿.ai'))
  expect(downloads).toEqual(['/api/files/9/download'])
  act(() => root.unmount()); query.clear(); root = createRoot(host)
  landing({ link_id: 'abc', kind: 'product', target_id: 3, status: 'voided', url: null, message: '该商品已作废' })
  await until(() => host.textContent!.includes('该商品已作废'))
  expect(requests).toContain('GET /api/l/abc?format=json')
})
