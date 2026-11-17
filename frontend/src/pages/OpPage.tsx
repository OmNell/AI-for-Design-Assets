import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getOp, withdrawOp } from '../api/ops'
import { resumeIngest } from '../api/audit'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { ChangePreviewPanel } from '../components/ops/ChangePreviewPanel'
import { RoundHistory } from '../components/review/ProductList'

const message = (reason: unknown) => reason instanceof Error ? reason.message : '操作未完成，请重试'

/** 入库后操作批次的详情：变更预览、审核往来、执行进度；责任人可以在终审通过前撤回。 */
export function OpPage() {
  const id = Number(useParams().id)
  const valid = Number.isInteger(id) && id > 0
  const client = useQueryClient()
  const op = useQuery({ queryKey: ['op', id], queryFn: () => getOp(id), enabled: valid, retry: false, refetchInterval: result => result.state.data?.status === '入库中' ? 1000 : false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!valid) return <GlassPanel><p role="alert">批次编号不正确</p></GlassPanel>
  if (op.isPending) return <GlassPanel><p>正在加载操作…</p></GlassPanel>
  if (op.isError) return <GlassPanel><p role="alert" className="text-red-800">{op.error.message}</p></GlassPanel>
  const value = op.data
  const act = (action: () => Promise<unknown>) => {
    setBusy(true); setError('')
    action().then(() => Promise.all([client.invalidateQueries({ queryKey: ['op', id] }), client.invalidateQueries({ queryKey: ['project', value.project_id] })]))
      .catch((reason: unknown) => setError(message(reason))).finally(() => setBusy(false))
  }
  const progress = value.progress
  const executing = ['入库中', '入库异常', '已入库'].includes(value.status)
  const statusText = value.status === '已入库' ? '已完成' : value.status === '入库中' ? '执行中' : value.status === '入库异常' ? '执行异常' : value.status
  return <div className="space-y-5">
    <GlassPanel><div className="flex items-start justify-between gap-6"><div className="min-w-0"><p className="text-xs tracking-widest text-emerald-800">入库后操作 · {value.kind_label}</p>
      <h1 className="mt-2 break-all text-xl font-semibold">{value.folder_name}</h1>
      <p className="mt-2 text-sm text-slate-600">{value.batch_no} · 发起人 {value.owner_name} · {new Date(value.created_at).toLocaleString('zh-CN')} · 第 {value.round} 轮 · <span data-testid="op-status" className="font-medium">{statusText}</span></p></div>
      <div className="flex shrink-0 items-center gap-3">{value.project_id !== null && <Link to={`/projects/${value.project_id}`} className="text-sm text-emerald-800 underline underline-offset-4">返回项目</Link>}
        {['待核对', '已驳回', '已暂存', '上传中', '上传中断', 'AI 处理中', 'AI 处理失败'].includes(value.status) && ['retag', 'supplement'].includes(value.kind) &&
          <Link to={['待核对', '已驳回'].includes(value.status) ? `/batches/${value.id}/review` : `/batches/${value.id}`} className="text-sm text-emerald-800 underline underline-offset-4">{['待核对', '已驳回'].includes(value.status) ? '去核对页' : '查看批次'}</Link>}
        {value.can_withdraw && <GlassButton primary={false} disabled={busy} onClick={() => { if (window.confirm(`确定撤回这次「${value.kind_label}」吗？`)) act(() => withdrawOp(id)) }}>撤回</GlassButton>}</div></div>
      {['待初审', '待终审'].includes(value.status) && <p className="mt-3 text-sm text-slate-500">已提交，等待{value.status === '待初审' ? '初审' : '终审'}。通过终审后由终审人的 NAS 账号执行。</p>}
      {value.status === '已驳回' && <p className="mt-3 text-sm text-amber-900">审核未通过。{['retag', 'supplement'].includes(value.kind) ? '可以到核对页修改后重新提交，或撤回。' : '请撤回后按审核意见重新发起。'}</p>}
      {executing && progress && <div data-testid="op-progress" className="mt-4 text-sm"><div className="h-2 overflow-hidden rounded-full bg-white/50"><div className="h-full bg-emerald-700 transition-all" style={{ width: `${progress.total ? progress.step / progress.total * 100 : 0}%` }} /></div>
        <p className="mt-2 text-slate-600">已完成 {progress.step} / {progress.total} 步{progress.current ? `，当前：${progress.current}` : ''}</p>
        {progress.error && <p role="alert" className="mt-2 text-red-800">{progress.error}</p>}
        {progress.can_resume && <GlassButton className="mt-3" disabled={busy} onClick={() => act(() => resumeIngest(id))}>继续执行</GlassButton>}
        {value.status === '入库异常' && !progress.can_resume && <p className="mt-2 text-slate-500">需要执行这次操作的终审人登录后点击「继续执行」。</p>}</div>}
      {error && <p role="alert" className="mt-3 text-sm text-red-800">{error}</p>}</GlassPanel>
    {value.preview && <GlassPanel><ChangePreviewPanel preview={value.preview} /></GlassPanel>}
    {!!value.history.length && <GlassPanel><h2 className="mb-3 text-lg font-semibold">审核往来</h2><RoundHistory history={value.history} /></GlassPanel>}
  </div>
}
