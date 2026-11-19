// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ArchiveRepairsPage, MaintenancePage } from './MaintenancePage'
import { GlassProvider } from '../components/glass/GlassProvider'
import { MODE_KEY } from '../components/glass/visualMode'

const response = (value: unknown) => new Response(JSON.stringify(value))
let container: HTMLDivElement
let root: ReturnType<typeof createRoot>
let cache: QueryClient

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  localStorage.setItem(MODE_KEY, 'lite')
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  act(() => root.unmount())
  cache.clear()
  container.remove()
  vi.unstubAllGlobals()
  localStorage.clear()
})

async function until(check: () => boolean) {
  for (let attempt = 0; attempt < 100; attempt++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    if (check()) return
  }
  throw new Error('界面没有显示预期结果')
}

function mount(path: string, repairs = false) {
  act(() => root.render(<QueryClientProvider client={cache}><GlassProvider><MemoryRouter initialEntries={[path]}>
    {repairs ? <ArchiveRepairsPage /> : <MaintenancePage />}
  </MemoryRouter></GlassProvider></QueryClientProvider>))
}

function button(text: string) {
  return [...container.querySelectorAll('button')].find(node => node.textContent === text)
}

it('核对临时文件后需要逐项清单和二次确认', async () => {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    calls.push(options?.method ?? 'GET')
    if (url.endsWith('/preview/fixture')) return response({ id: 1, target: 'B20260101-000001',
      entries: [{ path: '正式终止/资料.ai', size: 512, fingerprint: 'a'.repeat(64) }] })
    return response([{ key: 'fixture', label: 'B20260101-000001', status: '已终止', kind: 'ingest',
      owner: '模拟', files: 1, size: 512, eligible: true, updated_at: null }])
  }))
  mount('/admin/maintenance?tab=cleanup')
  await until(() => !!button('核对清单'))
  act(() => button('核对清单')?.click())
  await until(() => !!button('确认清理'))
  expect(container.textContent).toContain('正式终止/资料.ai')
  expect(button('确认清理')?.disabled).toBe(true)
  expect(calls.filter(method => method === 'POST')).toHaveLength(1)
})

it('全部档案维修建立独立草稿并指向审核入口', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'POST'
    ? response([{ project_id: 2, batch_id: 12, error: null }])
    : response([{ project_id: 2, project_code: 'P12', project_name: '样本', detail: '档案丢失', ready: true }])))
  mount('/archive/repairs', true)
  await until(() => !!button('为全部可申请项目创建草稿') && !button('为全部可申请项目创建草稿')?.disabled)
  act(() => button('为全部可申请项目创建草稿')?.click())
  await until(() => !!container.querySelector('a[href="/batches/12/operation"]'))
  expect(container.textContent).toContain('终审通过后由本轮终审本人执行')
})
