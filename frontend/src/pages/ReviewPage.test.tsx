// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ReviewPage, applyPatch, mergePatch } from './ReviewPage'
import { GlassProvider } from '../components/glass/GlassProvider'
import { MODE_KEY } from '../components/glass/visualMode'
import { useAuthStore } from '../stores/auth'
import type { Pending, ProductPatch, ReviewData, ReviewProduct } from '../api/review'

let root: Root
let host: HTMLDivElement
let query: QueryClient
let calls: { url: string; method: string; body: unknown }[]
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const tag = (value: string, extra: Partial<ReviewProduct['tags'][string]> = {}) => ({ value, terms: value ? value.split('、') : [], source: 'ai', ai_value: value, flag: null, ...extra })
const product = (id: number, name: string, kind: 'set' | 'item' = 'item', extra: Partial<ReviewProduct> = {}): ReviewProduct => ({ id, batch_file_id: id + 100, kind, display_name: name, file_name: `${name}.jpg`,
  fingerprint: 'f', thumb_status: 'done', thumb_error: null, ai_status: 'done', ai_error: null, manually_filled: false,
  tags: { category: tag('保温杯'), material: tag('陶瓷') }, free_text: 'AI 描述', free_text_source: 'ai', ai_free_text: 'AI 描述', flags: [], relation_items: [], relation_confirmed: false, ...extra })
const clean: Pending = { yellow: 0, relations_unconfirmed: 0, failed: 0, processing: 0, can_submit: true }
const review = (products: ReviewProduct[], pending: Partial<Pending> = {}, batch: Partial<ReviewData['batch']> = {}): ReviewData => ({
  batch: { id: 1, batch_no: 'B20261116-000001', project_code: 'P2026-0012', folder_name: 'P2026-0012_泸州老窖_中秋礼盒', company_name: '泸州老窖', project_name: '中秋礼盒', status: '待核对', won: true, round: 1,
    owner_id: 1, owner_name: 'one', uploader_id: 1, recommended_path: null, note_to_reviewer: null, editable: true, ...batch },
  fields: [{ key: 'category', label: '品类', hint: '这是什么东西' }, { key: 'material', label: '材质', hint: '主要材料' }],
  products, pending: { ...clean, ...pending },
  folders: ['01定稿文件', '02设计方案', '03拍摄照片及视频', '04授权文件', '05 详情页'].map((name, index) => ({ name, files: index ? [] : [{ name: '终稿.ai', size: 2048 }] })) })

/** 模拟服务端：保存 PATCH 的标签，其他写接口原样回传当前数据。 */
function serve(state: ReviewData) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const method = options.method ?? 'GET'
    if (url.includes('/thumbs/')) return response({ code: 'thumbnail_failed', message: '', detail: { retryable: false } }, 404)
    if (url.startsWith('/api/tag-values')) { calls.push({ url, method, body: null }); return response([{ term: '胡桃木', count: 3 }, { term: '陶瓷', count: 2 }]) }
    const body: unknown = typeof options.body === 'string' ? JSON.parse(options.body) : null
    if (method !== 'GET') calls.push({ url, method, body })
    const matched = /\/products\/(\d+)/.exec(url)
    const target = matched ? state.products.find(value => value.id === Number(matched[1])) : undefined
    if (method === 'PATCH' && target) {
      const patch = body as ProductPatch
      Object.assign(target, applyPatch(target, { ...patch, tags: Object.fromEntries(Object.entries(patch.tags ?? {}).map(([key, value]) => [key, value.trim()])) }))
      return response({ product: target, pending: state.pending })
    }
    if (method === 'PUT' && target) {
      target.relation_items = (body as { item_ids: number[] }).item_ids.map(id => ({ batch_product_id: id, source: 'manual' }))
      target.relation_confirmed = true
      state.pending = clean
      return response({ product: target, pending: state.pending })
    }
    if (url.endsWith('/submit')) state.batch = { ...state.batch, status: '待初审', editable: false }
    return response(state)
  }))
}
const settle = async (ms = 10) => { await act(async () => { await new Promise(resolve => setTimeout(resolve, ms)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 200; index++) { await settle(); if (condition()) return }
  throw new Error('界面没有完成预期更新')
}
function mount() {
  act(() => root.render(<QueryClientProvider client={query}><GlassProvider><MemoryRouter initialEntries={['/batches/1/review']}>
    <Routes><Route path="/batches/:id/review" element={<ReviewPage />} /></Routes></MemoryRouter></GlassProvider></QueryClientProvider>))
}
function remount() {
  act(() => root.unmount()); query.clear()
  root = createRoot(host); mount()
}
const button = (label: string, scope: ParentNode = document) => [...scope.querySelectorAll('button')].find(value => value.textContent === label)!
const field = (key: string) => host.querySelector<HTMLElement>(`[data-field="${key}"]`)!
const input = (key: string) => field(key).querySelector('input')!
function type(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  act(() => { Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value); element.dispatchEvent(new Event('input', { bubbles: true })) })
}
const ready = () => until(() => !!host.querySelector('[data-testid="pending"]'))

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.open = true } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.open = false } })
  localStorage.clear(); localStorage.setItem(MODE_KEY, 'lite')
  calls = []
  query = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  useAuthStore.getState().setUser({ id: 1, username: 'one', display_name: null, roles: ['uploader'], enabled: true })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); query.clear(); host.remove(); vi.unstubAllGlobals(); useAuthStore.getState().clear() })

it('顶部显示项目、位置待定和还差什么，未清零时提交按钮不可用', async () => {
  serve(review([product(1, '礼盒', 'set'), product(2, '酒瓶'), product(3, '月饼盒', 'item', { ai_status: 'failed', ai_error: '假模型调用失败', tags: {} })],
    { yellow: 5, relations_unconfirmed: 1, failed: 1, can_submit: false }))
  mount(); await ready()
  expect(host.textContent).toContain('P2026-0012 泸州老窖 中秋礼盒')
  expect(host.textContent).toContain('位置 位置待定')
  expect(host.querySelector('[data-testid="pending"]')!.textContent).toBe('待处理：黄色 5 项 · 套装关系未确认 1 个 · 打标失败 1 张')
  const submit = button('提交审核')
  expect(submit.disabled).toBe(true)
  expect(submit.title).toBe('提交前还需处理：黄色 5 项、套装关系未确认 1 个、打标失败 1 张')
  expect(host.querySelector('[data-testid="reject-banner"]')).toBeNull()
  const list = host.querySelector('[data-testid="product-list"]')!
  expect(list.textContent).toContain('套装（1）')
  expect(list.textContent).toContain('单品（2）')
  expect([...list.querySelectorAll('li')].find(row => row.textContent!.includes('月饼盒'))!.textContent).toContain('红')
  expect([...list.querySelectorAll('li')].find(row => row.textContent!.includes('礼盒'))!.textContent).toContain('黄')
  expect(host.querySelector('[data-testid="folders"]')!.textContent).toContain('终稿.ai2.0 KB')
  expect(host.querySelector('[data-testid="folders"]')!.textContent).toContain('05 详情页（0）')
})

it('修改字段后防抖自动保存，悬停可见 AI 原值，刷新后修改仍在', async () => {
  const state = review([product(2, '酒瓶')])
  serve(state); mount(); await ready()
  expect(field('material').querySelector('[data-source]')!.textContent).toBe('AI')
  type(input('material'), '竹、 胡桃木 ')
  expect(host.querySelector('[data-testid="save-status"]')!.textContent).toBe('有未保存的修改…')
  expect(calls.filter(call => call.method === 'PATCH')).toHaveLength(0)
  await until(() => host.querySelector('[data-testid="save-status"]')!.textContent === '所有修改已保存' && calls.some(call => call.method === 'PATCH'))
  expect(calls.filter(call => call.method === 'PATCH')).toEqual([{ url: '/api/batches/1/products/2', method: 'PATCH', body: { tags: { material: '竹、 胡桃木 ' } } }])
  const badge = field('material').querySelector<HTMLElement>('[data-source]')!
  expect(badge.textContent).toBe('已改')
  expect(badge.title).toBe('AI 原值：陶瓷 → 人工值：竹、 胡桃木')
  expect([...field('material').querySelectorAll('li')].map(item => item.firstChild!.textContent)).toEqual(['竹', '胡桃木'])
  remount(); await ready()
  expect(input('material').value).toBe('竹、 胡桃木')
  expect(field('material').querySelector<HTMLElement>('[data-source]')!.title).toContain('AI 原值：陶瓷')
})

it('小标签可删除，自由描述可修改，输入时提示库里用过的说法', async () => {
  serve(review([product(2, '酒瓶', 'item', { tags: { category: tag('保温杯'), material: tag('陶瓷、竹') } })]))
  mount(); await ready()
  act(() => field('material').querySelector<HTMLButtonElement>('[aria-label="删除 陶瓷"]')!.click())
  expect(input('material').value).toBe('竹')
  type(host.querySelector('textarea[maxlength="5000"]') as HTMLTextAreaElement, '人工描述')
  await until(() => calls.some(call => call.method === 'PATCH'))
  expect(calls.find(call => call.method === 'PATCH')!.body).toEqual({ tags: { material: '竹' }, free_text: '人工描述', confirm_fields: [], resolve_flags: [] })
  act(() => { input('material').focus() })
  type(input('material'), '竹、胡')
  await until(() => !!host.querySelector('[role="listbox"]'))
  expect(calls.some(call => call.url === `/api/tag-values?field=material&q=${encodeURIComponent('胡')}&batch_id=1`)).toBe(true)
  const option = [...host.querySelectorAll('[role="option"]')].find(value => value.textContent!.includes('胡桃木'))!
  expect(option.textContent).toContain('3 次')
  act(() => { option.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true })) })
  expect(input('material').value).toBe('竹、胡桃木')
})

it('标黄字段显示原因，点「确认 AI 值正确」立即保存并清除标记', async () => {
  serve(review([product(2, '酒瓶', 'item', { tags: { category: tag('无法判断', { flag: 'uncertain' }), material: tag('陶瓷') } })], { yellow: 1, can_submit: false }))
  mount(); await ready()
  expect(field('category').dataset.flag).toBe('uncertain')
  expect(field('category').textContent).toContain('模型写了「无法判断」')
  expect(button('确认 AI 值正确', field('material'))).toBeUndefined()
  act(() => button('确认 AI 值正确', field('category')).click())
  await until(() => calls.some(call => call.method === 'PATCH'))
  expect(calls.find(call => call.method === 'PATCH')!.body).toEqual({ confirm_fields: ['category'] })
  await until(() => field('category').dataset.flag === '')
  expect(field('category').querySelector('[data-source]')!.textContent).toBe('已确认')
})

it('套装图勾选包含的单品并确认关系，可切换并排视图', async () => {
  const state = review([product(1, '礼盒', 'set', { relation_items: [{ batch_product_id: 2, source: 'ai' }] }), product(2, '酒瓶'), product(3, '月饼盒')], { relations_unconfirmed: 1, can_submit: false })
  serve(state); mount(); await ready()
  const relations = host.querySelector('[data-testid="relations"]')!
  const boxes = [...relations.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
  expect(boxes.map(box => box.checked)).toEqual([true, false])
  expect(relations.textContent).toContain('AI 推荐的已勾选，请核对后确认')
  act(() => boxes[1].click())
  act(() => [...host.querySelectorAll('label')].find(label => label.textContent === '并排显示包含的单品')!.querySelector('input')!.click())
  expect([...host.querySelectorAll('[data-testid="side-items"] button')].map(item => item.lastChild!.textContent)).toEqual(['酒瓶', '月饼盒'])
  act(() => button('确认关系', relations).click())
  await until(() => calls.some(call => call.method === 'PUT'))
  expect(calls.find(call => call.method === 'PUT')).toEqual({ url: '/api/batches/1/products/1/relations', method: 'PUT', body: { item_ids: [2, 3] } })
  await until(() => host.querySelector('[data-testid="relations"]')!.textContent!.includes('关系已确认'))
  expect(button('提交审核').disabled).toBe(false)
})

it('多选三个单品后批量把材质改为同一个值', async () => {
  serve(review([product(1, '礼盒', 'set', { relation_confirmed: true }), product(2, '酒瓶'), product(3, '月饼盒'), product(4, '酒杯')]))
  mount(); await ready()
  expect(button('批量修改').disabled).toBe(true)
  act(() => button('全选单品').click())
  expect(host.textContent).toContain('已选 3 张')
  act(() => button('批量修改').click())
  const dialog = document.querySelector('dialog')!
  expect(dialog.textContent).toContain('批量修改 · 已选 3 张')
  const row = [...dialog.querySelectorAll('label')].find(label => label.textContent === '材质')!
  act(() => row.querySelector('input')!.click())
  type(dialog.querySelector<HTMLInputElement>('[aria-label="批量设置材质"]')!, '304 不锈钢')
  act(() => button('应用到 3 张图', dialog).click())
  await until(() => calls.some(call => call.url.endsWith('/bulk')))
  expect(calls.find(call => call.url.endsWith('/bulk'))!.body).toEqual({ product_ids: [2, 3, 4], tags: { material: '304 不锈钢' } })
  await until(() => !document.querySelector('dialog'))
})

it('失败的图可单张重试；移除主图需要确认', async () => {
  serve(review([product(2, '酒瓶', 'item', { ai_status: 'failed', ai_error: '假模型调用失败', tags: {} }), product(3, '月饼盒')], { failed: 1, can_submit: false }))
  mount(); await ready()
  expect(host.querySelector('[data-testid="tag-form"]')!.textContent).toContain('打标失败：假模型调用失败')
  expect(host.querySelector('[data-testid="tag-form"]')!.textContent).toContain('请单张重试，或直接在下方手工填写标签')
  act(() => button('重新打标').click())
  await until(() => calls.some(call => call.url.endsWith('/products/2/retry-ai')))
  act(() => button('移除这张图').click())
  const dialog = document.querySelector('dialog')!
  expect(dialog.textContent).toContain('它会同时从所有套装的「包含的单品」里去掉')
  expect(calls.some(call => call.method === 'DELETE')).toBe(false)
  act(() => button('确认移除', dialog).click())
  await until(() => calls.some(call => call.method === 'DELETE'))
  expect(calls.find(call => call.method === 'DELETE')!.url).toBe('/api/batches/1/products/2')
})

it('待处理清零后可填写备注并提交审核，提交后变为只读', async () => {
  serve(review([product(2, '酒瓶')]))
  mount(); await ready()
  expect(host.querySelector('[data-testid="pending"]')!.textContent).toBe('待处理项已清零，可以提交审核。')
  type(host.querySelector<HTMLTextAreaElement>('[aria-label="给审核的备注"]')!, '位置有异议')
  act(() => button('提交审核').click())
  await until(() => calls.some(call => call.url.endsWith('/submit')))
  expect(calls.find(call => call.url.endsWith('/submit'))!.body).toEqual({ note: '位置有异议' })
  await until(() => host.textContent!.includes('批次状态为「待初审」，当前为只读。'))
  expect(button('提交审核')).toBeUndefined()
  expect(input('material').disabled).toBe(true)
})

it('被驳回的批次预留顶部横幅；非责任人只读', async () => {
  serve(review([product(1, '礼盒', 'set'), product(2, '酒瓶')], {}, { status: '已驳回', round: 2, editable: false }))
  mount(); await ready()
  expect(host.querySelector('[data-testid="reject-banner"]')!.textContent).toContain('本批次已被驳回（第 2 轮）')
  expect(host.textContent).toContain('只有批次责任人和管理员可以修改，当前为只读。')
  expect([...host.querySelectorAll<HTMLInputElement>('[data-testid="tag-form"] input')].every(element => element.disabled)).toBe(true)
  expect(button('批量修改')).toBeUndefined()
  expect(button('确认关系')).toBeUndefined()
  expect(button('移除这张图')).toBeUndefined()
})

it('60 张主图的列表项在 lite 模式不挂玻璃滤镜，full 模式才使用', async () => {
  const many = Array.from({ length: 60 }, (_, index) => product(index + 1, `单品${index + 1}`))
  serve(review(many)); mount(); await ready()
  const rows = () => [...host.querySelectorAll('[data-testid="product-list"] li')]
  expect(rows()).toHaveLength(60)
  expect(rows().every(row => row.querySelector('[data-list-item="lite"]') && !row.querySelector('.glass-surface, .glass-effect, [data-glass-renderer]'))).toBe(true)
  localStorage.setItem(MODE_KEY, 'full')
  remount(); await ready()
  expect(rows().every(row => row.querySelector('.glass-surface[data-visual-mode="full"]'))).toBe(true)
})

it('草稿合并保留最新文字，界面预览把人工修改标成已改', () => {
  expect(mergePatch({ tags: { a: '1', b: '2' }, free_text: '旧' }, { tags: { a: '3' }, confirm_fields: ['c'] })).toEqual({ tags: { a: '3', b: '2' }, free_text: '旧', confirm_fields: ['c'], resolve_flags: [] })
  const preview = applyPatch(product(1, '杯子', 'item', { tags: { category: tag('无法判断', { flag: 'uncertain' }), material: tag('陶瓷') }, flags: ['multi_object'] }),
    { tags: { category: '保温杯', material: '陶瓷' }, free_text: '新描述', resolve_flags: ['multi_object'] })
  expect(preview.tags.category).toEqual({ value: '保温杯', terms: ['保温杯'], source: 'manual', ai_value: '无法判断', flag: null })
  expect(preview.tags.material.source).toBe('ai')
  expect(preview.free_text_source).toBe('manual')
  expect(preview.flags).toEqual([])
})
