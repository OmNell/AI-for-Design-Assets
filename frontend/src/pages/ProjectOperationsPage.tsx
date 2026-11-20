import { Button, Panel } from '../components/ui'
import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getOperationOptions, getVoidedProjects, operationLabels, startOperation, type LightOperation } from '../api/operations'
import { formatBytes } from '../lib/terms'

const linkClass = 'text-sm text-blue-700 underline underline-offset-4'
const summaries: Record<LightOperation, string> = {
  rename: '修改项目名、商品展示名或主图文件名。',
  retag: '核对并调整标签、自由描述和套装关系。',
  void: '让所选资产退出正式展示，保留历史文件和链接。',
  restore: '恢复已作废的项目、商品或文件。',
  move: '从目录树选择新的分类和存放位置。',
  purge: '对已作废满 30 天的对象发起物理删除审批。',
  archive_repair: '按卡片柜数据生成档案修复清单并提交审核。',
}

export function ProjectOperationsPage() {
  const id = Number(useParams().id)
  const valid = Number.isSafeInteger(id) && id > 0
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const cache = useQueryClient()
  const options = useQuery({ queryKey: ['project-operations', id], queryFn: () => getOperationOptions(id), enabled: valid, retry: false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const start = async (kind: LightOperation) => {
    if (busy) return
    setBusy(true); setError('')
    try {
      const result = await startOperation(id, kind)
      await cache.invalidateQueries({ queryKey: ['project-operations', id] })
      navigate(`/batches/${result.batch_id}/operation`, { state: { productId: Number(params.get('product')) || null, fileId: Number(params.get('file')) || null } })
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法创建操作批次') }
    finally { setBusy(false) }
  }
  if (!valid) return <Panel><p role="alert">项目编号不正确</p></Panel>
  if (options.isPending) return <Panel><p role="status">正在读取资产操作…</p></Panel>
  if (options.isError) return <Panel><p role="alert">{options.error.message}</p><Link to="/projects" className={linkClass}>返回项目列表</Link></Panel>
  const value = options.data
  const { before, kinds } = value
  const target = before.products.find(row => row.id === Number(params.get('product')))
  const historical = before.files.filter(row => ['voided', 'replaced', 'deleted'].includes(row.status))
  const voided = before.products.filter(row => ['voided', 'deleted'].includes(row.status))
  return <div className="space-y-5">
    <Panel><div className="space-y-4">
      <Link to={value.status === 'active' ? `/projects/${id}` : '/projects/voided'} className={linkClass}>{value.status === 'active' ? '返回项目详情' : '返回作废项目'}</Link>
      <div><h1 className="text-2xl font-semibold">资产操作</h1><p className="mt-2 text-sm text-slate-600">{before.project.project_code} · {before.project.company_name} · {before.project.project_name}{target && ` · ${target.display_name}`}</p></div>
      {value.status === 'voided' && <p role="status" className="rounded-xl bg-amber-100 p-3 text-sm text-amber-900">项目已作废</p>}
      {value.active_batch_id ? <p className="text-sm">该项目已有未完成的操作批次。 <Link className={linkClass} to={`/batches/${value.active_batch_id}`}>查看批次与进度</Link></p>
        : !kinds.length ? <p className="text-sm text-slate-500">当前账号没有可发起的操作。</p>
          : <div className="grid grid-cols-3 gap-3">{kinds.map(kind => <div key={kind} className="rounded-xl bg-slate-50 p-4">
            {kind === 'supplement' ? <Link className={linkClass} to={`/upload?project=${id}${target?.status === 'active' ? `&replace=${target.id}` : ''}`}>{target?.status === 'active' ? '替换主图' : operationLabels[kind]}</Link>
              : <Button primary={false} disabled={busy} onClick={() => void start(kind)}>{kind === 'purge' ? '申请物理删除' : operationLabels[kind]}</Button>}
            <p className="mt-2 text-sm text-slate-600">{kind === 'supplement' ? '可仅补资料，也可上传新商品或替换现有主图。' : summaries[kind]}</p>
          </div>)}</div>}
      {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    </div></Panel>
    {(voided.length > 0 || historical.length > 0 || value.status === 'voided') && <Panel>
      <h2 className="text-lg font-semibold">作废资产与历史文件</h2>
      <ul className="mt-3 divide-y divide-slate-400/20 text-sm">{(value.status === 'voided' ? before.products : voided).map(row => <li key={row.id} className="flex items-center justify-between py-2"><span>{row.product_code} {row.display_name}</span>
        {row.status === 'deleted' ? <span>已删除</span> : <Link to={`/files/${row.file_id}`} className={linkClass}>查看主图</Link>}</li>)}
        {(value.status === 'voided' ? before.files : historical).map(row => <li key={row.id} className="flex items-center justify-between gap-3 py-2"><span className="break-all">{row.rel_path} · {formatBytes(row.size)}</span>
          {row.status === 'deleted' ? <span className="shrink-0">已删除</span> : <Link to={`/files/${row.id}`} className={`shrink-0 ${linkClass}`}>查看文件</Link>}</li>)}</ul>
    </Panel>}
  </div>
}

export function VoidedProjectsPage() {
  const projects = useQuery({ queryKey: ['voided-projects'], queryFn: getVoidedProjects, retry: false })
  return <Panel><Link to="/projects" className={linkClass}>返回项目列表</Link><h1 className="my-4 text-2xl font-semibold">作废项目</h1>
    {projects.isPending ? <p role="status">正在读取作废项目…</p> : projects.isError ? <p role="alert">{projects.error.message}</p>
      : !projects.data.length ? <p className="text-sm text-slate-500">暂无可访问的作废项目。</p>
        : <ul className="divide-y divide-slate-400/20">{projects.data.map(row => <li key={row.project_id} className="py-3">
          <Link to={`/projects/${row.project_id}/operations`} className={linkClass}>{row.project_code} · {row.before.project.project_name} · {row.before.project.company_name}</Link>
          {row.before.project.voided_at && <p className="mt-1 text-xs text-slate-600">作废于 {new Date(row.before.project.voided_at).toLocaleString('zh-CN')}</p>}
        </li>)}</ul>}
  </Panel>
}
