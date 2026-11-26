// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { NasEntry } from '../api/nasBrowser'
import { AssetsPage } from './AssetsPage'

let host: HTMLDivElement
let root: Root
let store: QueryClient
const modified_at = '2026-11-20T03:00:00Z'
const folder: NasEntry = { name: '05 详情页', path: '05 详情页', is_dir: true, kind: 'folder', extension: '', size: 0, modified_at }
const image: NasEntry = { name: '礼盒 主图.jpg', path: '05 详情页/礼盒 主图.jpg', is_dir: false, kind: 'image', extension: 'jpg', size: 1024, modified_at }
const pdf: NasEntry = { name: '说明.pdf', path: '说明.pdf', is_dir: false, kind: 'file', extension: 'pdf', size: 64, modified_at }

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL: vi.fn(() => 'blob:preview'), revokeObjectURL: vi.fn() }))
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  store = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => { act(() => root.unmount()); store.clear(); host.remove(); vi.unstubAllGlobals() })

function mount() {
  act(() => root.render(<QueryClientProvider client={store}><MemoryRouter><AssetsPage /></MemoryRouter></QueryClientProvider>))
}
function button(name: string, container: ParentNode = host) {
  return [...container.querySelectorAll('button')].find(node => node.getAttribute('aria-label') === name || node.textContent?.trim() === name)
}
async function until(condition: () => boolean) {
  for (let index = 0; index < 50; index++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    if (condition()) return
  }
  throw new Error('界面未在预期时间完成更新')
}
const click = async (node: HTMLElement | undefined) => {
  expect(node).toBeDefined()
  await act(async () => node?.click())
}
const directory = (path = '', folders: NasEntry[] = [folder], files: NasEntry[] = [pdf]) =>
  ({ path, root_name: '设计验证', accessible: true, folders, files, total: folders.length + files.length, page: 1, page_size: 60 })

it('首次以 NAS 为来源，按需展开，进入文件夹后切换缩略图、读取详情和预览', async () => {
  const fetchMock = vi.fn(async (href: string) => {
    const url = new URL(href, 'http://localhost')
    const path = url.searchParams.get('path')
    if (url.pathname.endsWith('/list')) {
      if (url.searchParams.get('kind') === 'folder') return Response.json(directory('', [folder], []))
      return Response.json(path === folder.path ? directory(path, [], [image]) : directory())
    }
    if (url.pathname.endsWith('/info')) return Response.json({ ...image, exists: true, accessible: true, catalog_status: 'unmanaged' })
    return new Response('preview', { headers: { 'Content-Type': 'image/jpeg' } })
  })
  vi.stubGlobal('fetch', fetchMock)
  mount()
  await until(() => host.textContent!.includes(pdf.name))
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(host.querySelector('table[aria-label="NAS 文件列表"]')).not.toBeNull()
  await click(button('展开设计验证'))
  const tree = host.querySelector('nav[aria-label="NAS 目录树"]')!
  await until(() => Boolean(button(folder.name, tree)))
  await click(button(folder.name, tree))
  await until(() => host.textContent!.includes(image.name))
  await click(button('缩略图视图'))
  expect(host.querySelector('img[loading="lazy"]')?.getAttribute('src')).toContain('size=300')
  await click(button(image.name) ?? host.querySelector<HTMLButtonElement>('button[aria-pressed] img')?.closest('button') ?? undefined)
  await until(() => host.textContent!.includes('未纳入资产库') && host.querySelector('img[alt$="预览"]') !== null)
  const link = host.querySelector<HTMLAnchorElement>('a[href*="nas-browser/download"]')!
  const original = new URL(link.href).searchParams
  expect(original.get('path')).toBe(image.path)
  expect(original.get('transfer')).toMatch(/^[0-9a-f]{24}$/)
  link.addEventListener('click', event => event.preventDefault())
  await act(async () => link.click())
  const first = new URL(link.href).searchParams.get('transfer')
  await act(async () => link.click())
  expect(first).not.toBe(original.get('transfer'))
  expect(new URL(link.href).searchParams.get('transfer')).not.toBe(first)
  expect(fetchMock.mock.calls.some(([url]) => url.startsWith('/api/projects'))).toBe(false)
})

it('可重试目录连接错误，切换后独立加载已入库列表并保存选择', async () => {
  let reads = 0
  vi.stubGlobal('fetch', vi.fn(async (href: string) => {
    if (href.includes('/nas-browser/list')) {
      reads++
      return reads === 1 ? Response.json({ code: 'nas_unavailable', message: 'NAS 目录暂时不可用' }, { status: 503 })
        : Response.json(directory('', [], []))
    }
    if (href.includes('/catalog/tree')) return Response.json([])
    return Response.json({ items: [], total: 0, page: 1, page_size: 20 })
  }))
  mount()
  await until(() => host.textContent!.includes('NAS 目录暂时不可用'))
  await click(button('重试'))
  await until(() => host.textContent!.includes('当前目录为空'))
  await click(button('已入库资产'))
  await until(() => host.textContent!.includes('暂无已入库项目'))
  expect(localStorage.getItem('workbench.assets.mode')).toBe('catalog')
})
