// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AiSettingsPage } from './AiSettingsPage'
import { TagFieldsPage } from './TagFieldsPage'
import { BatchDetailPage } from './BatchDetailPage'
import { useAuthStore } from '../stores/auth'
import type { AiSettings, AiSettingsInput, TagFields, TagFieldsInput } from '../api/ai'
import type { Batch } from '../api/upload'

let root: Root
let host: HTMLDivElement
let query: QueryClient
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const settings: AiSettings = { base_url: 'https://example.test/v1', api_key_set: true, model_vision: 'vision', model_text: 'text', model_embed: 'embed', concurrency: 3, timeout_s: 60, retries: 3, stall_alert_min: 30 }
const fields: TagFields = { version: 1, fields: [{ key: 'category', label: '品类', hint: '这是什么东西', sort_order: 0, enabled: true }, { key: 'style', label: '风格', hint: '设计风格', sort_order: 1, enabled: true }] }
const batch = (status = 'AI 处理中'): Batch => ({ id: 1, batch_no: 'B1', project_code: 'P', folder_name: 'P_企业_项目', original_folder_name: 'P_企业_项目', status, queued: false, won: true, updated_at: '', files: [], directories: [], chunk_bytes: 8, parallel_files: 3, upload_token: null, differences: null, uploader_id: 1, owner_id: 1, recommended_path: null,
  ai_progress: { items_total: 6, items_done: 4, sets_total: 2, sets_done: 0, failed: 1 }, products: [] })
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 50; index++) { await settle(); if (condition()) return }
  throw new Error('界面没有完成预期更新')
}
function mount(node: ReactNode, route = '/') {
  act(() => root.render(<QueryClientProvider client={query}><MemoryRouter initialEntries={[route]}>{node}</MemoryRouter></QueryClientProvider>))
}
function detail() { mount(<Routes><Route path="/batches/:id" element={<BatchDetailPage />} /></Routes>, '/batches/1') }
function button(label: string) { return [...host.querySelectorAll('button')].find(value => value.textContent === label)! }
function type(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  act(() => { Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value); element.dispatchEvent(new Event('input', { bubbles: true })); element.dispatchEvent(new Event('change', { bubbles: true })) })
}
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  HTMLDialogElement.prototype.showModal ??= function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close ??= function () { this.removeAttribute('open') }
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  query = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  useAuthStore.getState().setUser({ id: 1, username: 'one', display_name: null, roles: ['uploader'], enabled: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); query.clear(); host.remove(); vi.unstubAllGlobals(); useAuthStore.getState().clear() })

it('设置页不回填密钥，提交立即清空，保存内容只含可写字段', async () => {
  let submitted: AiSettingsInput | undefined
  let finish: ((value: Response) => void) | undefined
  vi.stubGlobal('fetch', vi.fn((url: string, options: RequestInit) => {
    if (url === '/api/admin/vector-indexes') return Promise.resolve(response({ backend: 'numpy', indexes: [] }))
    if (url === '/api/admin/ai-alerts') return Promise.resolve(response([]))
    if (options?.method === 'PUT') { submitted = JSON.parse(String(options.body)) as AiSettingsInput; return new Promise<Response>(resolve => { finish = resolve }) }
    return Promise.resolve(response(settings))
  }))
  mount(<AiSettingsPage />)
  await until(() => !!host.querySelector('input[name="api_key"]'))
  const key = host.querySelector<HTMLInputElement>('input[name="api_key"]')!
  expect(key.value).toBe('')
  expect(host.textContent).toContain('已设置')
  type(key, 'private-key-for-test')
  await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
  expect(submitted?.api_key).toBe('private-key-for-test')
  expect(submitted).not.toHaveProperty('api_key_set')
  expect(key.value).toBe('')
  await act(async () => finish!(response(settings)))
  await until(() => host.textContent!.includes('AI 设置已保存'))
  expect(key.value).toBe('')
})

it('测试连接逐项展示成功和厂商错误，并显示停滞批次入口', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/settings/ai/test') return response({ results: [{ kind: 'vision', model: 'vision', success: true, error: null }, { kind: 'text', model: 'text', success: false, error: 'Invalid model name' }, { kind: 'embed', model: 'embed', success: true, error: null }] })
    if (url === '/api/admin/vector-indexes') return Promise.resolve(response({ backend: 'numpy', indexes: [] }))
    if (url === '/api/admin/ai-alerts') return response([{ id: 1, batch_id: 2, run_id: 1, reason: 'AI 处理长时间没有进展', detected_at: '2026-01-01' }])
    return response(settings)
  }))
  mount(<AiSettingsPage />)
  await until(() => !!button('测试连接'))
  act(() => button('测试连接').click())
  await until(() => host.textContent!.includes('Invalid model name'))
  expect(host.textContent).toContain('识图模型 · vision · 连接成功')
  expect(host.textContent).toContain('向量模型 · embed · 连接成功')
  expect(host.querySelector('a[href="/batches/2"]')).not.toBeNull()
})

it('固定字段页面只允许修改填写说明与顺序', async () => {
  let saved: TagFieldsInput | undefined
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => {
    if (options?.method === 'PUT') {
      saved = JSON.parse(String(options.body)) as TagFieldsInput
      return response({ version: 2, fields: saved.fields.map(value => ({ ...fields.fields.find(field => field.key === value.key), ...value })) })
    }
    return response(fields)
  }))
  mount(<TagFieldsPage />)
  await until(() => !!button('保存字段'))
  expect(host.textContent).toContain('11 个固定字段')
  expect(button('新增字段')).toBeUndefined()
  expect(button('删除')).toBeUndefined()
  expect(host.querySelector('input')).toBeNull()
  type(host.querySelector('textarea')!, '请填写实际看到的品类')
  act(() => host.querySelector<HTMLButtonElement>('[aria-label="上移风格"]')!.click())
  await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
  await until(() => host.textContent!.includes('标签字段已保存'))
  expect(saved).toEqual({ version: 1, fields: [
    { key: 'style', hint: '设计风格', sort_order: 0 },
    { key: 'category', hint: '请填写实际看到的品类', sort_order: 1 },
  ] })
  expect(host.textContent).toContain('当前版本 2')
})

it('字段版本冲突保留编辑内容并展示错误', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options: RequestInit) => options?.method === 'PUT' ? response({ code: 'version_conflict', message: '字段版本已变化，请刷新后重试' }, 409) : response(fields)))
  mount(<TagFieldsPage />)
  await until(() => !!button('保存字段'))
  const label = host.querySelector('textarea')!
  type(label, '改过的提示')
  await act(async () => { host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
  await until(() => host.textContent!.includes('字段版本已变化'))
  expect(label.value).toBe('改过的提示')
})

it('详情只读显示进度、字段标黄、图级标黄与套装推荐关系', async () => {
  const value = { ...batch('待核对'), tag_labels: { custom: '新增字段' }, products: [
    { id: 1, batch_file_id: 1, kind: 'item', display_name: '杯子', ai_status: 'done', ai_error: null, tags: { category: { value: '无法判断', terms: ['无法判断'], source: 'ai', ai_value: '无法判断', flag: 'uncertain' }, custom: { value: '新值', terms: ['新值'], source: 'ai', ai_value: '新值', flag: null } }, free_text: '一张商品图片', flags: ['multi_object'], relation_items: [], relation_confirmed: false },
    { id: 2, batch_file_id: 2, kind: 'set', display_name: '套装', ai_status: 'done', ai_error: null, tags: {}, free_text: '套装图片', flags: [], relation_items: [{ batch_product_id: 1, source: 'ai' }], relation_confirmed: false },
    { id: 3, batch_file_id: 3, kind: 'set', display_name: '空套装', ai_status: 'done', ai_error: null, tags: {}, free_text: '', flags: ['empty_relation'], relation_items: [], relation_confirmed: false },
  ] }
  vi.stubGlobal('fetch', vi.fn(async () => response(value)))
  detail()
  await until(() => host.textContent!.includes('推荐包含的单品'))
  expect(host.textContent).toContain('位置待定')
  expect(host.textContent).toContain('4 / 6')
  expect(host.textContent).toContain('一图识别到多个商品')
  expect(host.textContent).toContain('套装推荐清单为空')
  expect(host.textContent).toContain('品类 · 无法判断')
  expect(host.textContent).toContain('新增字段')
  expect(host.textContent).toContain('一张商品图片')
  expect(host.querySelector('input,textarea')).toBeNull()
})

it('整批失败可重试，随后填写原因并确认终止', async () => {
  const calls: string[] = []
  let reason: string | undefined
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit) => {
    calls.push(url)
    if (url.endsWith('/retry-ai')) return response(batch())
    if (url.endsWith('/cancel')) {
      reason = (JSON.parse(String(options.body)) as { reason: string }).reason
      return response({ ...batch('已终止'), termination_reason: reason })
    }
    return response(batch('AI 处理失败'))
  }))
  detail()
  await until(() => !!button('重试整批 AI'))
  act(() => button('重试整批 AI').click())
  await until(() => host.textContent!.includes('正在按单品、套装的顺序处理'))
  act(() => button('终止批次').click())
  const dialog = document.querySelector('dialog')!
  const submit = dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!
  expect(submit.disabled).toBe(true)
  type(dialog.querySelector('textarea')!, '改用另一批素材')
  expect(submit.disabled).toBe(false)
  await act(async () => submit.click())
  await until(() => host.textContent!.includes('已终止'))
  expect(calls).toContain('/api/batches/1/retry-ai')
  expect(calls).toContain('/api/batches/1/cancel')
  expect(reason).toBe('改用另一批素材')
  expect(button('终止批次')).toBeUndefined()
})

it('AI 处理时轮询详情，结束后停止轮询', async () => {
  vi.useFakeTimers()
  const fetcher = vi.fn().mockResolvedValueOnce(response(batch())).mockResolvedValueOnce(response(batch('待核对')))
  vi.stubGlobal('fetch', fetcher)
  try {
    detail()
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(host.textContent).toContain('AI 处理中')
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(host.textContent).toContain('待核对')
    await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
    expect(fetcher).toHaveBeenCalledTimes(2)
  } finally { vi.useRealTimers() }
})
