// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { PreDescriptionData, PreDescriptionInput, PreDescriptionProduct } from '../api/upload'
import { PreDescriptionPage } from './PreDescriptionPage'

let root: Root
let host: HTMLDivElement
let cache: QueryClient
const blank = (id: number, name: string, kind: PreDescriptionProduct['kind']): PreDescriptionProduct => ({
  id, kind, display_name: name, batch_file_id: id + 10, file_name: name + '.jpg', fingerprint: String(id).repeat(64),
  thumb_status: 'done', thumb_error: null, description: null, version: 0, state: 'missing',
  ai_status: 'awaiting_description', ai_error: null, visual_ai_succeeded: false, ai_in_progress: false,
})
const result = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })

function serve(state: PreDescriptionData) {
  const writes: { url: string; body?: PreDescriptionInput }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    if (url.includes('/thumbs/')) return result({ code: 'thumbnail_failed', detail: { retryable: false } }, 404)
    if (options?.method === 'PUT') {
      const body: PreDescriptionInput = JSON.parse(String(options.body))
      writes.push({ url, body })
      const product = state.products.find(product => url.includes('/products/' + product.id + '/'))!
      product.description = body.description
      product.version += 1
      product.state = 'ready'
      return result(state)
    }
    if (options?.method === 'POST') {
      writes.push({ url })
      return result({ id: 1, status: 'AI 处理中' })
    }
    return result(state)
  }))
  return writes
}

function mount() {
  act(() => root.render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={['/batches/1/pre-descriptions']}>
    <Routes><Route path="/batches/:id/pre-descriptions" element={<PreDescriptionPage />} />
      <Route path="/batches/:id" element={<p>已启动识别</p>} /></Routes>
  </MemoryRouter></QueryClientProvider>))
}

async function until(predicate: () => boolean) {
  for (let tries = 0; tries < 100; tries++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) })
    if (predicate()) return
  }
  throw new Error('预描述页面未完成更新')
}

const button = (label: string) => [...host.querySelectorAll('button')].find(value => value.textContent?.trim() === label)!
const text = () => host.querySelector('textarea')!
function type(value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(text(), value)
    text().dispatchEvent(new Event('input', { bubbles: true }))
  })
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); cache.clear(); host.remove(); vi.unstubAllGlobals() })

it('按主图分开保存，从未完成名单移除，并在刷新后用服务端进度启动 AI', async () => {
  const item = blank(1, '磁带音箱', 'item')
  const bundle = blank(2, '茶具礼盒', 'set')
  const writes = serve({ batch_id: 1, status: '已暂存', kind: 'ingest', folder_name: 'P1_企业_项目', editable: true, products: [item, bundle] })
  mount()
  await until(() => !!text())
  expect(button('确认全部描述并开始 AI').disabled).toBe(true)
  type('，，')
  expect(button('保存描述').disabled).toBe(true)
  type('磁带造型的蓝牙音箱')
  act(() => button('保存描述').click())
  await until(() => host.textContent!.includes('已完成 1 / 2'))
  expect(button('确认全部描述并开始 AI').disabled).toBe(true)
  expect(host.textContent).toContain('还需填写或确认：茶具礼盒')
  act(() => button('下一张').click())
  type('茶壶、茶杯礼盒')
  act(() => button('保存描述').click())
  await until(() => host.textContent!.includes('已完成 2 / 2'))
  expect(writes.map(write => write.body)).toEqual([
    { description: '磁带造型的蓝牙音箱', version: 0, fingerprint: item.fingerprint },
    { description: '茶壶、茶杯礼盒', version: 0, fingerprint: bundle.fingerprint },
  ])
  act(() => root.unmount())
  cache.clear()
  root = createRoot(host)
  mount()
  await until(() => text()?.value === '磁带造型的蓝牙音箱')
  expect(button('确认全部描述并开始 AI').disabled).toBe(false)
  act(() => button('确认全部描述并开始 AI').click())
  await until(() => host.textContent!.includes('已启动识别'))
  expect(writes.at(-1)?.url).toBe('/api/batches/1/confirm')
})

it('主图指纹变化后显示待重新确认，可以主动为当前图复用原文', async () => {
  const p = { ...blank(3, '新版酒瓶', 'item'), state: 'reconfirm' as const, version: 4, description: '酒瓶造型的香薰机' }
  const writes = serve({ batch_id: 1, status: '已暂存', kind: 'supplement', folder_name: 'P1_企业_项目', editable: true, products: [p] })
  mount()
  await until(() => !!text())
  expect(host.textContent).toContain('待重新确认')
  expect(button('确认全部描述并开始 AI').disabled).toBe(true)
  act(() => button('确认此图的描述').click())
  await until(() => !button('确认全部描述并开始 AI').disabled)
  expect(writes[0].body).toEqual({ description: '酒瓶造型的香薰机', version: 4, fingerprint: p.fingerprint })
})
