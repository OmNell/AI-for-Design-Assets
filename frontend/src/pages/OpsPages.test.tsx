// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ProjectPage } from './ProjectPage'
import { OpPage } from './OpPage'
import { BatchDetailPage } from './BatchDetailPage'
import { GlassProvider } from '../components/glass/GlassProvider'
import { MODE_KEY } from '../components/glass/visualMode'
import { useAuthStore } from '../stores/auth'
import { preflight } from '../lib/preflight'
import type { ProjectDetail, ProjectRow } from '../api/catalog'
import type { OpDetail } from '../api/ops'
import type { Role } from '../api/types'

let root: Root
let host: HTMLDivElement
let query: QueryClient
let requests: { call: string; body: unknown }[]
const row: ProjectRow = { id: 3, project_code: 'P001', project_name: '中秋礼盒', company_name: '企业', industry_name: '酒业', won: true,
  current_path: '01已中标项目/01酒业/07企业/P001_企业_中秋礼盒', status: 'active', ingested_at: '2026-11-10T02:00:00Z', products: 3, files: 4 }
const detail: ProjectDetail = { project: row, folder_name: 'P001_企业_中秋礼盒', code_source: 'company', link_id: 'L1', lifecycle: {}, open_batch: null,
  products_list: [{ id: 1, product_code: 'P001-001', display_name: '酒瓶', kind: 'item', status: 'active', link_id: 'a', related: [3], file_id: 101, lifecycle: {} },
    { id: 2, product_code: 'P001-002', display_name: '旧包装', kind: 'item', status: 'voided', link_id: 'b', related: [], file_id: 102, lifecycle: { void_reason: '拍错了' } },
    { id: 3, product_code: 'P001-003', display_name: '礼盒A', kind: 'set', status: 'active', link_id: 'c', related: [1], file_id: 103, lifecycle: {} }],
  folders: [{ name: '02设计方案', count: 1 }],
  history: [{ id: 5, file_id: 102, product_id: 2, product_code: 'P001-002', old_rel_path: '单品/旧包装.jpg', new_rel_path: '_历史版本/旧包装_20261117.jpg', reason: 'voided', created_at: '2026-11-17T02:00:00Z', file_status: 'voided', size: 10, link_id: null },
    { id: 6, file_id: 110, product_id: null, product_code: null, old_rel_path: '02设计方案/提案.pdf', new_rel_path: '_历史版本/提案_20261117.pdf', reason: 'replaced', created_at: '2026-11-17T02:00:00Z', file_status: 'replaced', size: 20, link_id: null }],
  logs: [] }
const op: OpDetail = { id: 9, batch_no: 'abc', kind: 'rename', kind_label: '改名', status: '待初审', project_id: 3, project_code: 'P001', folder_name: 'P001_企业_中秋礼盒', owner_id: 1, owner_name: '张三', round: 1,
  created_at: '2026-11-17T02:00:00Z', progress: null, can_withdraw: true, history: [],
  preview: { kind: 'rename', kind_label: '改名', reason: null, rows: [{ label: '项目名', before: '中秋礼盒', after: '中秋臻选' }], products: ['P001-001 酒瓶'], files: ['单品/酒瓶.jpg'], notes: ['编号和永久链接不变'] } }
const material = [{ id: 120, name: '提案.pdf', rel_path: '02设计方案/提案.pdf', size: 5, mtime: 1700000000000, status: 'active', category: '02设计方案', link_id: null }]

function serve(routes: (url: string, method: string) => unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const method = options.method ?? 'GET'
    requests.push({ call: `${method} ${url}`, body: typeof options.body === 'string' ? JSON.parse(options.body) : null })
    const result = routes(url, method)
    return result instanceof Response ? result : new Response(JSON.stringify(result), { status: 200 })
  }))
}
const settle = async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function until(condition: () => boolean) {
  for (let index = 0; index < 200; index++) { await settle(); if (condition()) return }
  throw new Error('界面没有完成预期更新')
}
function Where() { return <p data-testid="where">{useLocation().pathname}</p> }
function mount(node: ReactNode, route: string) {
  act(() => root.render(<QueryClientProvider client={query}><GlassProvider><MemoryRouter initialEntries={[route]}>{node}<Where /></MemoryRouter></GlassProvider></QueryClientProvider>))
}
const login = (...roles: Role[]) => useAuthStore.getState().setUser({ id: 1, username: 'me', display_name: null, roles, enabled: true })
const button = (label: string, scope: ParentNode = document) => [...scope.querySelectorAll('button')].find(value => value.textContent === label)!
const type = (label: string, value: string) => act(() => {
  const input = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[aria-label="${label}"]`)!
  Object.getOwnPropertyDescriptor(input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(input, value)
  input.dispatchEvent(new Event('input', { bubbles: true }))
})
const check = (text: string) => act(() => { [...document.querySelectorAll('label')].find(label => label.textContent?.includes(text))!.querySelector('input')!.click() })
const operations = () => [...host.querySelectorAll<HTMLButtonElement>('[aria-label="入库后操作"] button')].map(item => item.textContent)
const projectPage = <Routes><Route path="/projects/:id" element={<ProjectPage />} /><Route path="*" element={null} /></Routes>
const sent = (call: string) => requests.find(item => item.call === call)

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => false })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) { this.open = true }
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) { this.open = false }
  localStorage.clear(); localStorage.setItem(MODE_KEY, 'lite')
  requests = []
  query = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => { act(() => root.unmount()); query.clear(); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); useAuthStore.getState().clear() })

it('只有终审能看到移动、整项目作废、恢复和物理删除；没有上传或终审角色时不显示操作', async () => {
  serve(url => url.includes('/files?') ? material : detail)
  login('uploader')
  mount(projectPage, '/projects/3')
  await until(() => operations().length > 0)
  expect(operations()).toEqual(['补充', '改名', '修改标签', '作废'])
  act(() => login('uploader', 'final_reviewer'))
  await until(() => operations().length === 8)
  expect(operations()).toEqual(['补充', '改名', '修改标签', '作废', '移动', '整项目作废', '恢复', '物理删除'])
  act(() => login('first_reviewer'))
  await until(() => operations().length === 0)
})

it('项目有未结束的操作批次时显示提示并禁用全部操作', async () => {
  serve(url => url.includes('/files?') ? material : { ...detail, open_batch: { id: 9, batch_no: 'abc', kind: 'rename', kind_label: '改名', status: '待初审' } })
  login('uploader', 'final_reviewer')
  mount(projectPage, '/projects/3')
  await until(() => !!host.querySelector('[data-testid="open-batch"]'))
  expect(host.querySelector('[data-testid="open-batch"]')!.textContent).toContain('未结束的「改名」操作（abc · 待初审）')
  expect(host.querySelector('[data-testid="open-batch"] a')!.getAttribute('href')).toBe('/ops/9')
  expect([...host.querySelectorAll<HTMLButtonElement>('[aria-label="入库后操作"] button')].every(item => item.disabled)).toBe(true)
})

it('改名只提交改动过的名字，成功后进入操作详情页；服务端拒绝时在弹窗里显示原因', async () => {
  let reject = true
  serve((url, method) => url.endsWith('/ops/rename') && method === 'POST' ? reject ? new Response(JSON.stringify({ code: 'name_taken', message: '项目内已有同名文件：单品/酒瓶2.jpg' }), { status: 409 }) : op :
    url.includes('/files?') ? material : detail)
  login('uploader')
  mount(projectPage, '/projects/3')
  await until(() => operations().length > 0)
  act(() => button('改名').click())
  expect(button('提交改名').disabled).toBe(true)
  // 已作废的商品不能改名。
  expect(document.querySelector('[aria-label="P001-002 展示名"]')).toBeNull()
  type('项目名', '中秋臻选'); type('P001-001 展示名', '酒瓶2')
  act(() => button('提交改名').click())
  await until(() => !!document.querySelector('dialog [role="alert"]'))
  expect(document.querySelector('dialog [role="alert"]')!.textContent).toBe('项目内已有同名文件：单品/酒瓶2.jpg')
  reject = false
  act(() => button('提交改名').click())
  await until(() => host.querySelector('[data-testid="where"]')!.textContent === '/ops/9')
  expect(sent('POST /api/projects/3/ops/rename')!.body).toEqual({ project_name: '中秋臻选', products: [{ id: 1, name: '酒瓶2' }] })
})

it('修改标签建立批次后直接进入核对页；作废要选对象并写原因，勾了套装才出现级联选项', async () => {
  serve((url, method) => method === 'POST' ? { ...op, id: url.endsWith('/retag') ? 12 : 13 } : url.includes('/files?') ? material : detail)
  login('uploader')
  mount(projectPage, '/projects/3')
  await until(() => operations().length > 0)
  act(() => button('作废').click())
  await until(() => document.body.textContent!.includes('02设计方案/提案.pdf'))
  expect(button('提交作废').disabled).toBe(true)
  expect(document.body.textContent).not.toContain('连同套装包含的单品一起作废')
  check('P001-003 礼盒A'); check('02设计方案/提案.pdf'); check('连同套装包含的单品一起作废'); type('原因（必填）', '拍错了')
  act(() => button('提交作废').click())
  await until(() => host.querySelector('[data-testid="where"]')!.textContent === '/ops/13')
  expect(sent('POST /api/projects/3/ops/void')!.body).toEqual({ product_ids: [3], file_ids: [120], reason: '拍错了', cascade: true })
  act(() => root.unmount()); root = createRoot(host)
  mount(projectPage, '/projects/3')
  await until(() => operations().length > 0)
  act(() => button('修改标签').click())
  await until(() => host.querySelector('[data-testid="where"]')!.textContent === '/batches/12/review')
})

it('物理删除先预览文件和大小，冷静期内必须写例外原因，并手动输入编号确认', async () => {
  serve((url, method) => url.endsWith('/purge/preview') ? { files: [{ path: '_历史版本/旧包装_20261117.jpg', size: 2048 }], count: 1, total_size: 2048, confirm_label: '商品编号 P001-002', cooling_days: 7, in_cooling: true, earliest_allowed: '2026-11-24T02:00:00Z', summary: [] } :
    method === 'POST' ? { ...op, id: 15, kind: 'purge', kind_label: '物理删除', status: '入库中' } : url.includes('/files?') ? material : detail)
  login('final_reviewer')
  mount(projectPage, '/projects/3')
  await until(() => operations().length > 0)
  expect(operations()).toEqual(['移动', '整项目作废', '恢复', '物理删除'])
  act(() => button('物理删除').click())
  // 只列出已作废的商品和历史版本文件。
  expect(document.querySelector('dialog')!.textContent).toContain('已作废商品 P001-002 旧包装')
  expect(document.querySelector('dialog')!.textContent).toContain('历史版本 _历史版本/提案_20261117.pdf')
  expect(document.querySelector('dialog')!.textContent).not.toContain('酒瓶')
  check('已作废商品 P001-002'); act(() => button('查看将删除的文件').click())
  await until(() => !!document.querySelector('[data-testid="purge-preview"]'))
  expect(document.querySelector('[data-testid="purge-preview"]')!.textContent).toContain('将删除 1 个文件')
  expect(document.querySelector('[data-testid="purge-preview"]')!.textContent).toContain('需冷静 7 天')
  type('删除原因（必填）', '客户要求'); type('确认编号', 'P001-002')
  expect(button('确认物理删除').disabled).toBe(true)
  type('例外原因（冷静期内必填）', '法务要求立即删除')
  act(() => button('确认物理删除').click())
  await until(() => host.querySelector('[data-testid="where"]')!.textContent === '/ops/15')
  expect(sent('POST /api/projects/3/ops/purge')!.body).toEqual({ history_file_ids: [], product_ids: [2], project: false, reason: '客户要求', exception_reason: '法务要求立即删除', confirm: 'P001-002' })
})

it('操作详情页显示变更预览，责任人可以撤回；执行异常时终审可以继续', async () => {
  let current: OpDetail = op
  serve((url, method) => { if (method === 'POST' && url.endsWith('/withdraw')) current = { ...op, status: '已撤回', can_withdraw: false }; return current })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  login('uploader')
  mount(<Routes><Route path="/ops/:id" element={<OpPage />} /></Routes>, '/ops/9')
  await until(() => !!host.querySelector('[data-testid="change-preview"]'))
  const preview = host.querySelector('[data-testid="change-preview"]')!.textContent!
  for (const text of ['变更预览 · 改名', '项目名', '中秋礼盒', '中秋臻选', 'P001-001 酒瓶', '单品/酒瓶.jpg', '编号和永久链接不变']) expect(preview).toContain(text)
  expect(host.textContent).toContain('等待初审')
  act(() => button('撤回').click())
  await until(() => host.querySelector('[data-testid="op-status"]')!.textContent === '已撤回')
  expect(sent('POST /api/ops/9/withdraw')).toBeDefined()
  current = { ...op, status: '入库异常', can_withdraw: false, progress: { step: 1, total: 5, current: '移动与写入文件', error: '第 2 步（移动与写入文件）：操作人会话已失效', can_resume: true } }
  await act(async () => { await query.invalidateQueries() })
  await until(() => !!host.querySelector('[data-testid="op-progress"]'))
  expect(host.querySelector('[data-testid="op-progress"]')!.textContent).toContain('已完成 1 / 5 步，当前：移动与写入文件')
  act(() => button('继续执行').click())
  await until(() => !!sent('POST /api/review/9/resume-ingest'))
})

it('补充批次：冲突文件没选完不能确认，选择后提交到服务端', async () => {
  const file = (id: number, rel_path: string, conflict: string, conflict_choice: string | null) => ({ id, rel_path, size: 10, mtime: 1, upload_status: 'done', bytes_received: 10, fingerprint: 'f', thumb_status: null, thumb_error: null, conflict, conflict_choice, removed: false })
  const batch = (choice: string | null) => ({ id: 20, batch_no: 'sup', folder_name: 'P001_企业_中秋礼盒', original_folder_name: 'P001_企业_中秋礼盒', project_code: 'P001', status: '已暂存', queued: false, won: true, updated_at: '2026-11-17T02:00:00Z',
    kind: 'supplement', kind_label: '补充', project_id: 3, conflict_counts: { new: 1, duplicate: 1, conflict: 1, unresolved: choice ? 0 : 1 }, uploader_id: 1, directories: [], chunk_bytes: 1, parallel_files: 1, upload_token: null, differences: null,
    files: [file(1, '单品/新品.jpg', 'new', null), file(2, '单品/酒瓶.jpg', 'conflict', choice), file(3, '02设计方案/提案.pdf', 'duplicate', null)] })
  serve((url, method) => batch(method === 'PUT' && url.endsWith('/conflicts') ? 'keep_both' : null))
  login('uploader')
  mount(<Routes><Route path="/batches/:id" element={<BatchDetailPage />} /></Routes>, '/batches/20')
  await until(() => !!host.querySelector('[data-testid="conflict-counts"]'))
  expect(host.querySelector('[data-testid="conflict-counts"]')!.textContent).toBe('新文件 1 个（直接加入） · 内容相同 1 个（自动跳过） · 同名但内容不同 1 个，其中 1 个待选择')
  expect(host.querySelectorAll('[data-testid="conflict-file"]').length).toBe(1)
  expect(host.textContent).toContain('跳过：02设计方案/提案.pdf')
  expect(button('确认，开始 AI 处理').disabled).toBe(true)
  check('另存为新版本（原名_v2）')
  await until(() => !button('确认，开始 AI 处理').disabled)
  expect(sent('PUT /api/batches/20/conflicts')!.body).toEqual({ choices: { 2: 'keep_both' } })
})

it('编号已入库时本地预检不再要求主图和五个资料夹', () => {
  const entries = [{ rel_path: '02设计方案/提案.pdf', size: 1, mtime: 1, is_dir: false }]
  expect(preflight({ folder: 'P001_企业_中秋礼盒', entries }).map(issue => issue.rule_code).sort()).toEqual(['P07', 'P08', 'P08', 'P08', 'P08'])
  expect(preflight({ folder: 'P001_企业_中秋礼盒', entries, exists: true })).toEqual([])
})
