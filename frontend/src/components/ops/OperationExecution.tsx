import { Button, Panel } from '../ui'
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { executeOperation, getExecution, type OperationKind, type PurgePreview } from '../../api/operations'
import { formatBytes } from '../../lib/terms'

export function PurgeList({ preview }: { preview: Omit<PurgePreview, 'confirmation_code'> }) {
  return <section className="space-y-3 rounded-xl border border-red-300/70 bg-red-50/80 p-4" aria-label="预期物理删除清单">
    <h3 className="font-semibold text-red-900">预期物理删除清单</h3>
    <p className="text-sm">{preview.purge_files.length} 个文件 · 共 {formatBytes(preview.purge_files.reduce((total, file) => total + file.size, 0))}</p>
    {!!preview.product_codes?.length && <p className="text-sm">涉及编号　{preview.product_codes.join('、')}</p>}
    {!!preview.relationships?.length && <p className="text-sm">受影响关系　{preview.relationships.join('、')}</p>}
    <ul className="max-h-72 space-y-2 overflow-auto text-sm">{preview.purge_files.map(file =>
      <li key={file.path} className="border-t border-red-200 pt-2">
        <p className="break-all">{file.path} · {formatBytes(file.size)}{file.history && ' · 历史版本'}</p>
        <p className="break-all text-xs text-slate-600">文件 #{file.file_id} · SHA-256 {file.fingerprint || '—'}</p>
      </li>)}</ul>
    <p className="text-sm text-red-900">执行后，涉及的永久链接会显示「已删除」。物理文件将无法恢复或下载。</p>
  </section>
}

export function OperationExecution({ batchId, kind, projectId }: { batchId: number; kind: OperationKind; projectId?: number | null }) {
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const cache = useQueryClient()
  const key = ['operation-execution', batchId]
  const execution = useQuery({ queryKey: key, queryFn: () => getExecution(batchId), retry: false,
    refetchInterval: result => {
      const value = result.state.data
      return value && !value.can_resume && !value.error && !['已入库', '已完成', 'done', 'published'].includes(value.status) ? 1600 : false
    } })
  const run = async () => {
    if (busy) return
    setBusy(true); setError('')
    try {
      cache.setQueryData(key, await executeOperation(batchId, kind === 'purge' ? code.trim() : undefined))
      setCode('')
      await Promise.all([['batches'], ['batch', batchId], ['todos'], ['project'], ['projects']].map(queryKey => cache.invalidateQueries({ queryKey })))
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法继续执行') }
    finally { setBusy(false) }
  }
  if (execution.isPending) return <Panel><p role="status">正在获取执行进度…</p></Panel>
  if (execution.isError) return <Panel><p role="alert">{execution.error.message}</p></Panel>
  const value = execution.data
  return <Panel><div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-semibold">操作执行进度</h2>
      {projectId && <Link to={`/projects/${projectId}/operations`} className="text-sm text-blue-700 underline">返回资产操作</Link>}</div>
    <p role="status">{value.status} · {value.step_name || `第 ${value.step} 步`}</p>
    {value.error && <p className="rounded-xl bg-amber-100 p-3 text-sm text-amber-950">{value.error}</p>}
    {kind === 'purge' && !!value.purge_files?.length && <PurgeList preview={value} />}
    {error && <p role="alert" className="text-sm text-red-900">{error}</p>}
    {value.can_resume && (kind === 'purge' ? <div className="space-y-3">
      <label className="block text-sm">再次输入编号 <strong>{value.confirmation_code}</strong> 确认执行
        <input className="field mt-2 max-w-sm" value={code} onChange={event => setCode(event.target.value)} autoComplete="off" /></label>
      <Button disabled={busy || !value.confirmation_code || code.trim() !== value.confirmation_code} onClick={() => void run()}>确认执行物理删除</Button>
    </div> : <Button disabled={busy} onClick={() => void run()}>从检查点继续执行</Button>)}
  </div></Panel>
}
