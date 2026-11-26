import { Button, Panel } from '../components/ui'
import { useState } from 'react'
import { Link, Navigate, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { checkFile, getFindings, getLink } from '../api/catalog'
import { ApiError } from '../api/client'
import { AssetPreview } from '../components/AssetPreview'
import { CopyLink } from '../components/CopyLink'
import { formatBytes } from '../lib/terms'
import { describeError, triggerDownload } from '../lib/download'

function Notice({ message, projectId }: { message: string; projectId?: number }) {
  return <Panel><p role="alert" className="text-lg font-semibold">{message}</p>
    <Link to="/projects" className="mt-4 inline-block text-sm text-blue-700 underline">返回项目列表</Link>{projectId && <Link to={`/projects/${projectId}/operations`} className="ml-4 text-sm text-blue-700 underline">资产操作与历史</Link>}</Panel>
}

function FileView({ id, url, historical, operations }: { id: number; url?: string; historical?: boolean; operations?: string }) {
  const query = useQuery({ queryKey: ['catalog-file', id], queryFn: () => checkFile(id) })
  const [show, setShow] = useState(false)
  const [error, setError] = useState('')
  if (query.isPending) return <Panel><p role="status">正在检查文件权限…</p></Panel>
  if (query.isError) return <Notice message={query.error instanceof ApiError && query.error.status === 410 ? '该文件已物理删除' : query.error.message} />
  const file = query.data
  const download = () => {
    if (file.warning && !window.confirm(`${file.warning}\n仍要下载当前 NAS 文件？`)) return
    try { triggerDownload(file.url) } catch (reason) { setError(describeError(reason)) }
  }
  return <div className="space-y-4"><Panel>
    {(historical || file.status === 'voided' || file.status === 'replaced') &&
      <p role="status" className="mb-4 rounded-xl bg-amber-100 p-3 text-sm text-amber-900">{file.status === 'replaced' ? '这是历史文件版本' : '该资产已作废'}，请确认后使用。</p>}
    <h1 className="break-all text-2xl font-semibold">{file.name}</h1>
    <p className="mt-2 text-sm text-slate-600">{file.is_image ? '图片' : (file.name.includes('.') ? file.name.split('.').at(-1)?.toUpperCase() : '文件')} · {formatBytes(file.size)}</p>
    {file.warning && <p role="alert" className="mt-3 rounded-xl bg-amber-100 p-3 text-sm text-amber-900">{file.warning}</p>}
    <div className="mt-4 flex flex-wrap items-center gap-2">
      {file.is_image && <Button primary={false} onClick={() => setShow(value => !value)}>{show ? '关闭预览' : '预览图片'}</Button>}
      <Button onClick={download}>下载文件</Button>
      {url && <CopyLink url={url} />}
      {operations && <Link to={operations} className="text-sm text-blue-700 underline">资产操作与历史</Link>}
    </div>
    {error && <p role="alert" className="mt-3 text-red-800">{error}</p>}
  </Panel>{show && <Panel><AssetPreview fileId={id} name={file.name} warning={file.warning} /></Panel>}</div>
}

export function FilePage() {
  const id = Number(useParams().id)
  return Number.isSafeInteger(id) && id > 0 ? <FileView key={id} id={id} /> : <Notice message="文件编号不正确" />
}

export function AssetLinkPage() {
  const { linkId = '' } = useParams()
  const valid = /^[a-zA-Z0-9_-]+$/.test(linkId)
  const query = useQuery({ queryKey: ['asset-link', linkId], queryFn: () => getLink(linkId), enabled: valid })
  if (!valid) return <Notice message="永久链接不正确" />
  if (query.isPending) return <Panel><p role="status">正在打开永久链接…</p></Panel>
  if (query.isError) return <Notice message={query.error.message} />
  const target = query.data
  if (target.status === 'deleted') {
    const date = target.deleted_at ? ` · ${new Date(target.deleted_at).toLocaleString('zh-CN')}` : ''
    return <Notice message={`该资产已物理删除${date}${target.delete_reason ? ` · ${target.delete_reason}` : ''}`} />
  }
  if (target.kind === 'file') return <FileView key={target.target_id} id={target.target_id} url={target.url} historical={target.status === 'voided'} operations={`/projects/${target.project_id}/operations?file=${target.target_id}`} />
  if (target.status === 'voided') {
    if (target.image_file_id) return <FileView key={target.image_file_id} id={target.image_file_id} url={target.url} historical operations={`/projects/${target.project_id}/operations${target.kind === 'product' ? `?product=${target.target_id}` : ''}`} />
    return <Notice message="该项目已作废，可使用已有的文件永久链接访问获准的历史文件。" projectId={target.project_id} />
  }
  return <Navigate to={`/${target.kind === 'project' ? 'projects' : 'products'}/${target.target_id}`} replace />
}

export function FindingsPage() {
  const findings = useQuery({ queryKey: ['asset-findings'], queryFn: getFindings })
  const names: Record<string, string> = { moved: '违规移动', modified: '内容变化', missing: '未找到文件' }
  return <Panel><h1 className="text-xl font-semibold">正式文件异常</h1>
    {findings.isPending ? <p className="mt-4">正在读取记录…</p> :
      findings.isError ? <p role="alert" className="mt-4">{findings.error.message}</p> :
      !findings.data.length ? <p className="mt-4 text-slate-500">暂无记录。</p> :
      <ul className="mt-4 space-y-3">{findings.data.map(row => <li key={row.id} className="border-t border-slate-400/20 pt-3 text-sm">
        <strong>{names[row.kind] ?? row.kind}</strong> · {new Date(row.created_at).toLocaleString('zh-CN')} · 文件 #{row.file_id}
        <p className="mt-1 text-slate-600">{row.detail}</p>
      </li>)}</ul>}
  </Panel>
}
