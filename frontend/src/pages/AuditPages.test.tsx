// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AuditPage } from './AuditPage'
import { QueuePage, formatWaiting } from './QueuePage'
import { CompanyMapPage } from './CompanyMapPage'
import { HomePage } from './HomePage'
import { ReviewPage } from './ReviewPage'
import { GlassProvider } from '../components/glass/GlassProvider'
import { MODE_KEY } from '../components/glass/visualMode'
import { useAuthStore } from '../stores/auth'
import type { AuditData } from '../api/audit'
import type { ReviewData, ReviewProduct } from '../api/review'
import type { Role } from '../api/types'

let root: Root
let host: HTMLDivElement
let query: QueryClient
let calls: { url: string; method: string; body: unknown }[]
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })
const product = (id: number, name: string, kind: 'set' | 'item' = 'item', extra: Partial<ReviewProduct> = {}): ReviewProduct => ({ id, batch_file_id: id + 100, kind, display_name: name, file_name: `${name}.jpg`,
  fingerprint: 'f', thumb_status: 'done', thumb_error: null, ai_status: 'done', ai_error: null, visual_ai_succeeded: true,
  tags: { category: { value: '保温杯', terms: ['保温杯'], source: 'ai', ai_value: '保温杯', flag: null } }, free_text: '', free_text_source: 'ai', ai_free_text: '', flags: [], relation_items: [], relation_confirmed: true, relation_confirm_kind: "confirmed_members", ...extra })
const reviewData = (status: string, extra: Partial<ReviewData> = {}): ReviewData => ({
  batch: { id: 1, batch_no: 'B1', project_code: 'P2026-0012', folder_name: 'P2026-0012_泸州老窖_中秋礼盒', company_name: '泸州老窖', project_name: '中秋礼盒', status, won: true, round: 1,
    owner_id: 1, owner_name: 'one', uploader_id: 1, recommended_path: null, note_to_reviewer: null, review_snapshot_hash:"fixture", editable: status === '已驳回' },
  fields: [{ key: 'category', label: '品类', hint: '这是什么东西' }], products: [product(1, '礼盒', 'set', { relation_items: [{ batch_product_id: 2, source: 'ai' }] }), product(2, '酒瓶')],
  pending: { fields_unconfirmed:0, invalid_files:0, missing_main_image:false, yellow: 0, relations_unconfirmed: 0, failed: 0, processing: 0, can_submit: true }, folders: [], history: [], ...extra })
const auditData = (extra: Partial<AuditData> = {}): AuditData => ({ version:"fixture", review: reviewData('待初审'), stage: 'first', claimed_by: { id: 1, name: 'me' }, claimed_by_me: true, can_claim: false, blocked: false, can_edit_path: false,
  path: { recommended: '01已中标项目/01酒业/07泸州老窖', final: null, effective: '01已中标项目/01酒业/07泸州老窖', company_name: '泸州老窖', company_in_tree: true },
  acks: ['缺少 04授权文件 · 上传人已知晓'], stats: { manual_edits: 6, ai_confirmed: 3, confirmed_unknown: 1, appended: 1, removed: 0 },
  relations: [{ set_id: 1, set_name: '礼盒', items: ['酒瓶'], ai_count: 1, manual_count: 0, confirmed: true }], note: '请留意位置',
  folder_counts: [{ name: '01定稿文件', count: 12 }, { name: '05 详情页', count: 3 }], history: [], pins: [],
  checklist: { path: '存放位置正确', naming: '项目命名与编号无误', one_product: '一图一品', relations: '套装与单品关系正确', tags: '标签合理', content: '无不当内容' }, ...extra })

function serve(routes: (url: string, method: string, body: unknown) => unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const method = options.method ?? 'GET'
    if (url.includes('/thumbs/')) return response({ code: 'thumbnail_failed', message: '', detail: { retryable: false } }, 404)
    const body: unknown = typeof options.body === 'string' ? JSON.parse(options.body) : null
    if (method !== 'GET') calls.push({ url, method, body })
    const result = routes(url, method, body)
    return result instanceof Response ? result : response(result)
  }))
}
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 150; index++) { await settle(); if (condition()) return }
  throw new Error('界面没有完成预期更新')
}
function mount(node: ReactNode, route: string) {
  act(() => root.render(<QueryClientProvider client={query}><GlassProvider><MemoryRouter initialEntries={[route]}>{node}</MemoryRouter></GlassProvider></QueryClientProvider>))
}
const audit = () => mount(<Routes><Route path="/review/:id" element={<AuditPage />} /></Routes>, '/review/1')
const button = (label: string, scope: ParentNode = document) => [...scope.querySelectorAll('button')].find(value => value.textContent === label)!
function type(element: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
  act(() => { Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(element, value); element.dispatchEvent(new Event('input', { bubbles: true })) })
}
const login = (roles: Role[]) => useAuthStore.getState().setUser({ id: 1, username: 'me', display_name: null, roles, enabled: true })

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function (this: HTMLDialogElement) { this.open = true } })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.open = false } })
  localStorage.clear(); localStorage.setItem(MODE_KEY, 'lite')
  calls = []
  query = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  login(['first_reviewer'])
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); query.clear(); host.remove(); vi.unstubAllGlobals(); useAuthStore.getState().clear() })

it('审核信息面板按示意展示，初审没有修改位置的控件，标签页只读', async () => {
  serve(() => auditData()); audit()
  await until(() => !!host.querySelector('[data-testid="audit-info"]'))
  const info = host.querySelector('[data-testid="audit-info"]')!.textContent!
  expect(info).toContain('01已中标项目 / 01酒业 / 07泸州老窖')
  expect(info).toContain('缺少 04授权文件 · 上传人已知晓')
  expect(info).toContain('人工修改 6 处 · 确认 AI 值 3 处 · 明确留空 1 项 · 追加 1 张')
  expect(info).toContain('礼盒 → 酒瓶（AI 推荐 1，人工补 0）')
  expect(info).toContain('“请留意位置”')
  expect(info).toContain('01定稿文件 12 个 · 05 详情页 3 个')
  expect(button('修改')).toBeUndefined()
  act(() => button('标签（只读）').click())
  expect([...host.querySelectorAll<HTMLInputElement>('[data-field] input')].every(input => input.disabled)).toBe(true)
  expect(button('确认关系')).toBeUndefined()
})

it('审核清单未勾全不能通过，勾全后提交六项', async () => {
  serve((url) => url.endsWith('/pass') ? auditData({ review: reviewData('待终审'), stage: 'final', claimed_by: null, claimed_by_me: false }) : auditData()); audit()
  await until(() => !!button('通过'))
  expect(button('通过').disabled).toBe(true)
  const boxes = [...host.querySelectorAll<HTMLInputElement>('[data-testid="audit-info"] input[type="checkbox"]')]
  expect(boxes).toHaveLength(6)
  boxes.slice(0, 5).forEach(box => act(() => box.click()))
  expect(button('通过').disabled).toBe(true)
  act(() => boxes[5].click())
  act(() => button('通过').click())
  await until(() => host.textContent!.includes('初审已通过，批次进入「待终审」。'))
  expect(calls.find(call => call.url.endsWith('/pass'))!.body).toEqual({ checklist: { path: true, naming: true, one_product: true, relations: true, tags: true, content: true } })
})

it('未领取时只能领取；本轮初审人被告知不能终审', async () => {
  serve((url) => url.endsWith('/claim') ? auditData() : auditData({ claimed_by: null, claimed_by_me: false, can_claim: true })); audit()
  await until(() => !!button('领取'))
  expect(button('通过')).toBeUndefined()
  expect(button('钉意见')).toBeUndefined()
  act(() => button('领取').click())
  await until(() => host.querySelector('[data-testid="claim-state"]')!.textContent === '你已领取本批次，可以审核。')
  act(() => root.unmount()); query.clear(); root = createRoot(host)
  serve(() => auditData({ review: reviewData('待终审'), stage: 'final', claimed_by: null, claimed_by_me: false, blocked: true })); audit()
  await until(() => !!host.querySelector('[data-testid="claim-state"]'))
  expect(host.querySelector('[data-testid="claim-state"]')!.textContent).toBe('本账号须回避该阶段审核。')
  expect(button('领取')).toBeUndefined()
})

it('驳回必须填写总体意见，并连同钉图意见一起发出', async () => {
  const pins = [{ batch_product_id: 2, product_name: '酒瓶', x: 0.2, y: 0.4, text: '瓶盖颜色不对' }]
  serve((url) => url.endsWith('/reject') ? auditData({ review: reviewData('已驳回'), stage: null, claimed_by: null, claimed_by_me: false }) : auditData({ pins })); audit()
  await until(() => !!button('驳回'))
  expect(host.querySelector<HTMLInputElement>('[aria-label="第 1 条钉图意见"]')!.value).toBe('瓶盖颜色不对')
  expect([...host.querySelectorAll('[data-testid="product-list"] li')].find(row => row.textContent!.includes('酒瓶'))!.textContent).toContain('钉 1')
  act(() => button('驳回').click())
  const dialog = document.querySelector('dialog')!
  expect(dialog.textContent).toContain('将连同 1 条钉图意见一起发出')
  expect(button('确认驳回', dialog).disabled).toBe(true)
  type(dialog.querySelector('textarea')!, '标签不合理')
  act(() => button('确认驳回', dialog).click())
  await until(() => calls.some(call => call.url.endsWith('/reject')))
  expect(calls.map(call => [call.url, call.body])).toEqual([['/api/review/1/reject', { comment: '标签不合理', pins: [{ batch_product_id: 2, x: 0.2, y: 0.4, text: '瓶盖颜色不对' }] }]])
  await until(() => host.textContent!.includes('已驳回，批次退回上传人。'))
})

it('终审可从目录树修改位置；位置待定且企业不在对照表时通过按钮禁用并提示', async () => {
  login(['final_reviewer'])
  const pending = auditData({ review: reviewData('待终审'), stage: 'final', can_edit_path: true, path: { recommended: null, final: null, effective: null, company_name: '新客户', company_in_tree: false } })
  const tree = [{ won: true, folder: '01已中标项目', industries: [{ id: 3, name: '酒业', folder: '01酒业', companies: [{ id: 9, name: '泸州老窖', folder: '07泸州老窖' }] }] }, { won: false, folder: '02未中标项目', industries: [] }]
  serve((url) => url === '/api/catalog/tree' ? tree : url.endsWith('/path') ? auditData({ version: '调整后', review: reviewData('待终审'), stage: 'final', can_edit_path: true }) : pending); audit()
  await until(() => !!host.querySelector('[data-testid="path-blocked"]'))
  expect(host.textContent).toContain('位置待定——企业「新客户」不在对照表中')
  expect(host.querySelector('[data-testid="path-blocked"]')!.textContent).toBe('位置待定：等待管理员在对照表中录入该企业。')
  host.querySelectorAll<HTMLInputElement>('[data-testid="audit-info"] section input[type="checkbox"]').forEach(box => act(() => box.click()))
  expect(button('通过').disabled).toBe(true)
  act(() => button('修改').click())
  await until(() => !!host.querySelector('[data-testid="path-picker"] select'))
  const selects = host.querySelectorAll<HTMLSelectElement>('[data-testid="path-picker"] select')
  act(() => { selects[0].value = '3'; selects[0].dispatchEvent(new Event('change', { bubbles: true })) })
  act(() => { const companies = host.querySelectorAll<HTMLSelectElement>('[data-testid="path-picker"] select')[1]; companies.value = '9'; companies.dispatchEvent(new Event('change', { bubbles: true })) })
  act(() => button('保存位置').click())
  await until(() => calls.some(call => call.url.endsWith('/path')))
  expect(calls.find(call => call.url.endsWith('/path'))!.body).toEqual({ won: true, company_id: 9 })
  await until(() => !host.querySelector('[data-testid="path-blocked"]'))
  expect(button('通过').disabled).toBe(true)
  host.querySelectorAll<HTMLInputElement>('[data-testid="audit-info"] section input[type="checkbox"]').forEach(box => act(() => box.click()))
  expect(button('通过').disabled).toBe(false)
})

it('队列显示上传人、商品数、文件量、等待时长、轮次与领取状态', async () => {
  serve(() => [{ id: 7, batch_no: 'B7', folder_name: 'P1_企业_项目', round: 2, uploader_name: 'one', products: 12, files: 40, total_size: 3 * 1024 * 1024, waiting_since: '', waiting_seconds: 90000, claimed_by: { id: 5, name: '张三' }, claimed_by_me: false, blocked: false },
    { id: 8, batch_no: 'B8', folder_name: 'P2_企业_项目', round: 1, uploader_name: 'two', products: 1, files: 1, total_size: 10, waiting_since: '', waiting_seconds: 120, claimed_by: null, claimed_by_me: false, blocked: true }])
  mount(<QueuePage stage="final" />, '/final-review')
  await until(() => host.querySelectorAll('tbody tr').length === 2)
  const rows = [...host.querySelectorAll('tbody tr')].map(row => row.textContent!)
  expect(rows[0]).toContain('one'); expect(rows[0]).toContain('40 个 · 3.0 MB'); expect(rows[0]).toContain('1 天 1 小时'); expect(rows[0]).toContain('第 2 轮'); expect(rows[0]).toContain('张三 已领取')
  expect(rows[1]).toContain('本账号须回避')
  expect(host.querySelector('a[href="/review/7"]')).not.toBeNull()
  expect([formatWaiting(30), formatWaiting(7200)]).toEqual(['1 分钟', '2 小时'])
})

it('对照表页新增行业和企业，别名按顿号拆分后保存', async () => {
  login(['admin'])
  const saved = { industries: [{ id: 1, name: '酒业', seq: 1 }, { id: 2, name: '金融', seq: 2 }], companies: [{ id: 1, name: '茅台', aliases: ['贵州茅台'], industry_id: 1, industry_name: '酒业', seq: 1 }, { id: 2, name: '招商银行', aliases: ['招行', 'CMB'], industry_id: 2, industry_name: '金融', seq: 1 }] }
  serve((_url, method) => method === 'PUT' ? saved : { industries: [saved.industries[0]], companies: [saved.companies[0]] })
  mount(<CompanyMapPage />, '/admin/company-map')
  await until(() => !!button('新增行业'))
  expect(host.textContent).toContain('只增不重排')
  act(() => button('新增行业').click())
  type(host.querySelector<HTMLInputElement>('[aria-label="行业 2 名称"]')!, '金融')
  act(() => button('新增企业').click())
  type(host.querySelector<HTMLInputElement>('[aria-label="企业 2 标准名"]')!, '招商银行')
  type(host.querySelector<HTMLInputElement>('[aria-label="企业 2 别名"]')!, '招行、CMB')
  act(() => { const select = host.querySelector<HTMLSelectElement>('[aria-label="企业 2 所属行业"]')!; select.value = '金融'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  act(() => button('保存对照表').click())
  await until(() => host.textContent!.includes('对照表已保存'))
  expect(calls[0].body).toEqual({ industries: [{ id: 1, name: '酒业' }, { name: '金融' }],
    companies: [{ id: 1, name: '茅台', aliases: ['贵州茅台'], industry_name: '酒业' }, { name: '招商银行', aliases: ['招行', 'CMB'], industry_name: '金融' }] })
  expect(host.querySelectorAll('tbody tr')).toHaveLength(2)
})

it('首页顶部待办条显示数量并直达对应页面', async () => {
  login(['uploader', 'first_reviewer'])
  serve((url) => url === '/api/todos' ? [{ key: 'rejected', label: '已驳回', count: 1, link: '/batches/5/review', batches: [{ id: 5, label: 'P_企业_项目' }] }, { key: 'first_review', label: '待初审', count: 3, link: '/first-review', batches: [] }] :
    url === '/api/batches' ? [] : { version: '0.1.0', mode: 'dev', database_available: true })
  mount(<HomePage />, '/')
  await until(() => !!host.querySelector('[data-testid="todos"]'))
  const links = [...host.querySelectorAll<HTMLAnchorElement>('[data-testid="todos"] a')]
  expect(links.map(link => [link.getAttribute('href'), link.textContent])).toEqual([['/batches/5/review', '已驳回1'], ['/first-review', '待初审3']])
})

it('核对页的驳回横幅显示总体意见，被钉的图有标记并可撤回、查看历次意见', async () => {
  login(['uploader'])
  const history = [{ round: 2, stage: 'final', reviewer_name: '李四', decision: 'reject', comment: '位置不对，请核实', decided_at: null, pins: [{ batch_product_id: 2, product_name: '酒瓶', x: 0.5, y: 0.5, text: '这里有划痕' }] },
    { round: 2, stage: 'first', reviewer_name: '张三', decision: 'pass', comment: null, decided_at: null, pins: [] }]
  const data = reviewData('已驳回', { history })
  data.batch.round = 2
  serve((url) => url.endsWith('/cancel') ? { status: '已终止' } : data)
  mount(<Routes><Route path="/batches/:id/review" element={<ReviewPage />} /><Route path="/" element={<p>首页</p>} /></Routes>, '/batches/1/review')
  await until(() => !!host.querySelector('[data-testid="reject-banner"]'))
  const banner = host.querySelector('[data-testid="reject-banner"]')!
  expect(banner.textContent).toContain('本批次已被驳回（第 2 轮） · 终审 李四')
  expect(banner.textContent).toContain('总体意见：位置不对，请核实')
  expect(banner.textContent).toContain('查看历次意见（2）')
  expect(banner.textContent).toContain('酒瓶：这里有划痕')
  expect([...host.querySelectorAll('[data-testid="product-list"] li')].find(row => row.textContent!.includes('酒瓶'))!.textContent).toContain('钉 1')
  act(() => button('终止批次', banner).click())
  const dialog = document.querySelector('dialog')!
  expect(button('确认终止', dialog).disabled).toBe(true)
  type(dialog.querySelector('textarea')!, '不再入库')
  act(() => button('确认终止', dialog).click())
  await until(() => host.textContent === '首页')
  expect(calls.find(call => call.url.endsWith('/cancel'))!.url).toBe('/api/batches/1/cancel')
})

it('等待正式向量时显示入库异常，终审继续后显示已入库', async () => {
  login(['final_reviewer'])
  serve((url) => {
    const base = { step: 8, step_name: '生成向量并发布', products: 3, files: 4, vectors_ready: 2 }
    if (url.endsWith('/ingest')) return { ...base, status: '入库异常', error: '1 个商品的正式向量未生成', can_resume: true }
    if (url.endsWith('/resume-ingest')) return { ...base, status: '已入库', step: 9, step_name: '入库完成', vectors_ready: 3, error: null, can_resume: false }
    return auditData({ review: reviewData('入库异常'), stage: null, claimed_by_me: false })
  })
  audit()
  await until(() => !!button('继续入库'))
  expect(host.textContent).toContain('正式向量 2 / 3')
  expect(host.textContent).toContain('1 个商品的正式向量未生成')
  act(() => button('继续入库').click())
  await until(() => host.textContent!.includes('正式向量 3 / 3'))
  expect(host.textContent).toContain('已入库')
  expect(button('继续入库')).toBeUndefined()
})
