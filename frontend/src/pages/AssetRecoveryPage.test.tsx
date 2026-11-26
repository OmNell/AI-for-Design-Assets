// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AssetRecoveryPage } from './AssetRecoveryPage'
import type { RecoveryRun } from '../api/recovery'

let host: HTMLElement
let root: Root
let query: QueryClient

const base = '/api/admin/recovery'
const now = '2026-11-26T10:00:00Z'
const selection = (action: RecoveryRun['action'], id: number, status = 'done'): RecoveryRun => ({
  id, action, status, actor: 'admin', error: null, started_at: now, finished_at: status === 'done' ? now : null,
  progress: {}, summary: {},
})
const status = (runs: RecoveryRun[]) => ({
  catalog_state: 'empty_unconfirmed', nas_state: 'system_access_required', nas_checked_at: now,
  projects: 0, products: 0, files: 0, can_backup: false, last_backup_at: null, runs,
})
const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })
const button = (name: string) => [...host.querySelectorAll('button')].find(node => node.textContent?.trim() === name)
const click = (name: string) => act(() => button(name)!.click())
const mount = () => act(() => root.render(<QueryClientProvider client={query}>
  <MemoryRouter><AssetRecoveryPage /></MemoryRouter>
</QueryClientProvider>))

async function until(condition: () => boolean | undefined) {
  for (let i = 0; i < 50; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)) })
    if (condition()) return
  }
  throw new Error('资产恢复界面没有更新')
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  document.body.append(host)
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount())
  query.clear()
  host.remove()
  vi.unstubAllGlobals()
})

it('空数据库恢复引导、差异审核和显式确认后执行计划', async () => {
  const scan = selection('scan', 1)
  const rows: RecoveryRun[] = []
  const fetch = vi.fn(async (path: string) => {
    if (path === base) return response(status(rows))
    if (path === base + '/scan') { rows.push(scan); return response(scan) }
    if (path === base + '/reports/1') return response({
      items: [
        { kind: 'missing', project_id: 11, code: 'P011', path: '正式区/P011', version: 0, reason: '本地尚未登记' },
        { kind: 'conflict', project_id: 12, code: 'P012', path: '正式区/P012', version: 3, reason: '存在未完成业务' },
      ], unresolved: [],
    })
    if (path === base + '/apply') {
      const queued = { ...selection('apply', 2, 'running'), progress: { phase: 'indexing', code: 'P011', products: 3 } }
      rows.unshift(queued)
      return response(queued)
    }
    if (path.endsWith('/vector-indexes')) return response({ backend: 'numpy', indexes: [] })
    return response({ total: 0, ready: 0, missing: 0, running: false })
  })
  vi.stubGlobal('fetch', fetch)
  mount()
  await until(() => !!button('扫描 NAS 已入库资产'))
  expect(host.textContent).toContain('本地尚未恢复历史资产')
  click('扫描 NAS 已入库资产')
  await until(() => !!button('执行 1 个项目的恢复计划'))
  expect(host.textContent).toContain('存在未完成业务')
  expect(host.textContent).toContain('待人工确认')
  click('执行 1 个项目的恢复计划')
  expect(fetch).not.toHaveBeenCalledWith(base + '/apply', expect.anything())
  click('确认执行')
  await until(() => host.textContent?.includes('正在核对和恢复向量 · P011 · 3 个商品'))
  expect(fetch).toHaveBeenCalledWith(base + '/apply',
    expect.objectContaining({ method: 'POST', body: '{"plan_id":1,"confirmed":true}' }))
})

it('从候选暂存已核验备份，并能提交缺失向量和缩略图任务', async () => {
  const detect = selection('detect', 1)
  const stage = selection('stage', 2)
  const rows = [detect]
  const indexes = { backend: 'numpy', indexes: [{ generation: 1, status: 'formal', ready: 1, failures: 0,
    pending: 1, running: false, estimated_calls: 1 }] }
  const fetch = vi.fn(async (path: string) => {
    if (path === base) return response(status(rows))
    if (path === base + '/reports/1') return response({ backup_state: 'checked', backups: [{
      name: 'wb-backup-20261126-1830.zip', size: 2000, format: 1, validation: 'manifest_checked', modified_at: now,
    }, {
      name: 'wb-backup-20261126-1820.zip', size: 500, format: 1, validation: 'empty', modified_at: now,
    }] })
    if (path === base + '/stage') { rows.unshift(stage); return response(stage) }
    if (path === base + '/reports/2') return response({
      stage_dir: '/stage/ready', source_revision: '0022', revision: '0023', difference: { items: [] },
    })
    if (path.includes('/vector-indexes')) return response(indexes)
    if (path.endsWith('/status')) return response({ total: 2, ready: 1, missing: 1, running: false })
    return response([{ id: 3 }])
  })
  vi.stubGlobal('fetch', fetch)
  mount()
  await until(() => !!button('选择备份') && !button('重建缺失缩略图')?.disabled && !button('补齐正式检索索引')?.disabled)
  expect(host.textContent).toContain('优先核验')
  const empty = [...host.querySelectorAll('tr')].find(row => row.textContent?.includes('空库备份，待核对'))
  expect(empty?.querySelector('button')?.disabled).toBe(true)
  click('重建缺失缩略图')
  click('补齐正式检索索引')
  await until(() => fetch.mock.calls.some(([path]) => path.endsWith('/vector-indexes/1/retry')))
  expect(fetch).toHaveBeenCalledWith('/api/admin/thumbnails/rebuild',
    expect.objectContaining({ method: 'POST', body: '{"product_ids":[],"missing_only":true,"all_missing":true}' }))
  click('选择备份')
  expect(fetch).not.toHaveBeenCalledWith(base + '/stage', expect.anything())
  click('确认执行')
  await until(() => host.textContent?.includes('/stage/ready'))
  expect(host.textContent).toContain('0022 → 0023')
  expect(fetch).toHaveBeenCalledWith(base + '/stage',
    expect.objectContaining({ method: 'POST', body: '{"backup_name":"wb-backup-20261126-1830.zip","confirmed":true}' }))
})
