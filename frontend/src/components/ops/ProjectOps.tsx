import { useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getProjectFiles, type FileRow, type ProjectDetail } from '../../api/catalog'
import { moveProject, previewPurge, purge, renameProject, restoreItems, retagProject, voidItems, voidProject, type OpDetail, type PurgePreview, type PurgeTargets } from '../../api/ops'
import { formatBytes } from '../../lib/terms'
import { useAuthStore } from '../../stores/auth'
import { GlassButton } from '../glass/GlassButton'
import { GlassModal } from '../glass/GlassModal'
import { PathPicker } from '../audit/PathPicker'

type Op = 'rename' | 'void' | 'void-project' | 'restore' | 'move' | 'purge'
interface FormProps { detail: ProjectDetail; busy: boolean; run: (action: () => Promise<OpDetail>) => void }
const message = (reason: unknown) => reason instanceof Error ? reason.message : '操作未完成，请重试'
const titles: Record<Op, string> = { rename: '改名', void: '作废', 'void-project': '整项目作废', restore: '恢复', move: '移动项目', purge: '物理删除' }

function Reason({ value, onChange, label = '原因（必填）' }: { value: string; onChange: (value: string) => void; label?: string }) {
  return <label className="block text-sm"><span className="text-slate-600">{label}</span>
    <textarea aria-label={label} className="field mt-1 !rounded-xl !px-3 !py-2 text-sm" rows={2} maxLength={500} value={value} onChange={event => onChange(event.target.value)} /></label>
}
function Check({ label, checked, onChange }: { label: ReactNode; checked: boolean; onChange: (on: boolean) => void }) {
  return <label className="flex items-start gap-2 py-0.5 text-sm"><input type="checkbox" className="mt-1" checked={checked} onChange={event => onChange(event.target.checked)} /><span className="min-w-0 break-all">{label}</span></label>
}
const toggled = (set: Set<number>, id: number, on: boolean) => { const next = new Set(set); if (on) next.add(id); else next.delete(id); return next }

function RenameForm({ detail, busy, run }: FormProps) {
  const products = detail.products_list.filter(item => item.status === 'active')
  const [project, setProject] = useState(detail.project.project_name)
  const [names, setNames] = useState<Record<number, string>>({})
  const changed = products.filter(item => names[item.id] !== undefined && names[item.id].trim() !== item.display_name)
  const projectChanged = project.trim() !== detail.project.project_name
  return <div className="space-y-4">
    <p className="text-xs text-slate-500">立项编号、企业名和商品编号都不变，永久链接照常可用。提交后经初审、终审通过才会生效。</p>
    <label className="block text-sm"><span className="text-slate-600">项目名</span>
      <input aria-label="项目名" className="field mt-1 !rounded-xl !px-3 !py-2 text-sm" maxLength={60} value={project} onChange={event => setProject(event.target.value)} /></label>
    <div className="max-h-64 space-y-2 overflow-auto pr-1">{products.map(item => <label key={item.id} className="grid grid-cols-[88px_minmax(0,1fr)] items-center gap-2 text-sm"><span className="text-xs text-slate-500">{item.product_code}</span>
      <input aria-label={`${item.product_code} 展示名`} className="field !rounded-xl !px-3 !py-1.5 text-sm" maxLength={100} value={names[item.id] ?? item.display_name} onChange={event => setNames({ ...names, [item.id]: event.target.value })} /></label>)}</div>
    <GlassButton disabled={busy || (!projectChanged && !changed.length) || !project.trim()} onClick={() => run(() => renameProject(detail.project.id, { project_name: projectChanged ? project.trim() : null,
      products: changed.map(item => ({ id: item.id, name: names[item.id].trim() })) }))}>提交改名</GlassButton>
  </div>
}

/** 项目里不属于任何商品的有效资料文件。 */
function useMaterialFiles(detail: ProjectDetail) {
  const owned = new Set(detail.products_list.map(item => item.file_id))
  const names = detail.folders.map(item => item.name)
  return useQuery({ queryKey: ['project-materials', detail.project.id, names.join('|')], retry: false,
    queryFn: async (): Promise<FileRow[]> => (await Promise.all(names.map(name => getProjectFiles(detail.project.id, name)))).flat(),
    select: rows => rows.filter(row => row.status === 'active' && !owned.has(row.id)) })
}

function VoidForm({ detail, busy, run }: FormProps) {
  const products = detail.products_list.filter(item => item.status === 'active')
  const files = useMaterialFiles(detail)
  const [productIds, setProductIds] = useState<Set<number>>(new Set())
  const [fileIds, setFileIds] = useState<Set<number>>(new Set())
  const [cascade, setCascade] = useState(false)
  const [reason, setReason] = useState('')
  const hasSet = products.some(item => item.kind === 'set' && productIds.has(item.id))
  return <div className="space-y-4">
    <p className="text-xs text-slate-500">作废后文件移入「_历史版本」，商品从搜索中消失，编号保留不再使用；终审可以恢复。</p>
    <div className="max-h-56 overflow-auto pr-1"><p className="text-xs text-slate-500">商品</p>
      {products.map(item => <Check key={item.id} label={`${item.product_code} ${item.display_name}（${item.kind === 'set' ? '套装' : '单品'}）`} checked={productIds.has(item.id)} onChange={on => setProductIds(toggled(productIds, item.id, on))} />)}
      <p className="mt-3 text-xs text-slate-500">资料文件</p>
      {files.isPending ? <p className="text-xs text-slate-500">正在加载文件…</p> : files.isError ? <p role="alert" className="text-xs text-red-800">{files.error.message}</p> :
        files.data.length ? files.data.map(file => <Check key={file.id} label={file.rel_path} checked={fileIds.has(file.id)} onChange={on => setFileIds(toggled(fileIds, file.id, on))} />) : <p className="text-xs text-slate-500">（没有资料文件）</p>}</div>
    {hasSet && <Check label="连同套装包含的单品一起作废（不勾选则单品保留）" checked={cascade} onChange={setCascade} />}
    <Reason value={reason} onChange={setReason} />
    <GlassButton disabled={busy || !reason.trim() || (!productIds.size && !fileIds.size)} onClick={() => run(() => voidItems(detail.project.id, { product_ids: [...productIds], file_ids: [...fileIds], reason: reason.trim(), cascade: hasSet && cascade }))}>提交作废</GlassButton>
  </div>
}

function VoidProjectForm({ detail, busy, run }: FormProps) {
  const [reason, setReason] = useState('')
  return <div className="space-y-4">
    <p className="text-sm">整个项目文件夹将移入「/设计资产/_已作废项目/」，全部商品从搜索中消失。项目和商品的编号保留，终审可以恢复。</p>
    <Reason value={reason} onChange={setReason} />
    <GlassButton disabled={busy || !reason.trim()} onClick={() => run(() => voidProject(detail.project.id, reason.trim()))}>提交整项目作废</GlassButton>
  </div>
}

function RestoreForm({ detail, busy, run }: FormProps) {
  const whole = detail.project.status === 'voided'
  const products = detail.products_list.filter(item => item.status === 'voided')
  const files = detail.history.filter(item => item.reason === 'voided' && item.file_status === 'voided' && item.product_id === null)
  const [productIds, setProductIds] = useState<Set<number>>(new Set())
  const [fileIds, setFileIds] = useState<Set<number>>(new Set())
  const [reason, setReason] = useState('')
  return <div className="space-y-4">
    {whole ? <p className="text-sm">整个项目将从「_已作废项目」移回作废前的位置，随项目作废的商品一并恢复。</p> :
      <div className="max-h-56 overflow-auto pr-1">{products.map(item => <Check key={item.id} label={`${item.product_code} ${item.display_name}`} checked={productIds.has(item.id)} onChange={on => setProductIds(toggled(productIds, item.id, on))} />)}
        {files.map(item => <Check key={item.id} label={`${item.new_rel_path}（原位置 ${item.old_rel_path}）`} checked={fileIds.has(item.file_id)} onChange={on => setFileIds(toggled(fileIds, item.file_id, on))} />)}
        {!products.length && !files.length && <p className="text-sm text-slate-500">这个项目没有已作废的内容。</p>}</div>}
    <Reason value={reason} onChange={setReason} />
    <GlassButton disabled={busy || !reason.trim() || (!whole && !productIds.size && !fileIds.size)} onClick={() => run(() => restoreItems(detail.project.id, { product_ids: [...productIds], file_ids: [...fileIds], project: whole, reason: reason.trim() }))}>提交恢复</GlassButton>
  </div>
}

function MoveForm({ detail, busy, run }: FormProps) {
  const [reason, setReason] = useState('')
  return <div className="space-y-3">
    <p className="text-xs text-slate-500">当前位置：{detail.project.current_path}。新位置只能从目录树里选；编号和永久链接不变。</p>
    <Reason value={reason} onChange={setReason} />
    {reason.trim() ? <PathPicker won={detail.project.won} busy={busy} onCancel={() => setReason('')} onSave={(won, company) => run(() => moveProject(detail.project.id, { won, company_id: company, reason: reason.trim() }))} />
      : <p className="text-xs text-amber-900">先填写原因，再选择新位置。</p>}
  </div>
}

function PurgeForm({ detail, busy, run }: FormProps) {
  const whole = detail.project.status === 'voided'
  const products = detail.products_list.filter(item => item.status === 'voided')
  const files = detail.history.filter(item => ['voided', 'replaced'].includes(item.file_status) && !(item.file_status === 'voided' && item.product_id !== null))
  const [productIds, setProductIds] = useState<Set<number>>(new Set())
  const [fileIds, setFileIds] = useState<Set<number>>(new Set())
  const [preview, setPreview] = useState<PurgePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reason, setReason] = useState('')
  const [exception, setException] = useState('')
  const [confirm, setConfirm] = useState('')
  const targets: PurgeTargets = { history_file_ids: [...fileIds], product_ids: [...productIds], project: whole }
  const load = async () => {
    setLoading(true); setError('')
    try { setPreview(await previewPurge(detail.project.id, targets)) } catch (reason: unknown) { setError(message(reason)) } finally { setLoading(false) }
  }
  const reset = () => { setPreview(null); setConfirm('') }
  return <div className="space-y-4">
    <p className="rounded-xl bg-red-100/70 px-3 py-2 text-sm text-red-900">物理删除不可恢复，只能删除已作废的内容。由终审本人立即执行，不再经过审核。</p>
    {whole ? <p className="text-sm">将删除整个项目文件夹（连同档案）。</p> :
      <div className="max-h-44 overflow-auto pr-1">{products.map(item => <Check key={item.id} label={`已作废商品 ${item.product_code} ${item.display_name}`} checked={productIds.has(item.id)} onChange={on => { setProductIds(toggled(productIds, item.id, on)); reset() }} />)}
        {files.map(item => <Check key={item.id} label={`历史版本 ${item.new_rel_path}`} checked={fileIds.has(item.file_id)} onChange={on => { setFileIds(toggled(fileIds, item.file_id, on)); reset() }} />)}
        {!products.length && !files.length && <p className="text-sm text-slate-500">这个项目没有可删除的已作废内容或历史版本。</p>}</div>}
    {!preview && <GlassButton primary={false} disabled={loading || (!whole && !productIds.size && !fileIds.size)} onClick={() => void load()}>查看将删除的文件</GlassButton>}
    {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    {preview && <div data-testid="purge-preview" className="space-y-3">
      <div className="rounded-xl bg-white/40 p-3 text-sm"><p className="font-medium">将删除 {preview.count} 个文件，共 {formatBytes(preview.total_size)}</p>
        <ul className="mt-2 max-h-32 space-y-0.5 overflow-auto text-xs">{preview.files.map(file => <li key={file.path} className="break-all">{file.path}（{formatBytes(file.size)}）</li>)}</ul></div>
      {preview.in_cooling && <><p className="text-sm text-amber-900">作废后需冷静 {preview.cooling_days} 天才能删除{preview.earliest_allowed ? `（最早 ${new Date(preview.earliest_allowed).toLocaleDateString('zh-CN')}）` : ''}。要提前删除必须填写例外原因。</p>
        <Reason value={exception} onChange={setException} label="例外原因（冷静期内必填）" /></>}
      <Reason value={reason} onChange={setReason} label="删除原因（必填）" />
      <label className="block text-sm"><span className="text-slate-600">请输入{preview.confirm_label}以确认</span>
        <input aria-label="确认编号" className="field mt-1 !rounded-xl !px-3 !py-2 text-sm" autoComplete="off" value={confirm} onChange={event => setConfirm(event.target.value)} /></label>
      <GlassButton disabled={busy || !reason.trim() || !confirm.trim() || (preview.in_cooling && !exception.trim())}
        onClick={() => run(() => purge(detail.project.id, { ...targets, reason: reason.trim(), exception_reason: exception.trim(), confirm: confirm.trim() }))}>确认物理删除</GlassButton>
    </div>}
  </div>
}

/** 项目页的入库后操作：一个项目同时只能有一个未结束的操作批次。 */
export function ProjectOps({ detail }: { detail: ProjectDetail }) {
  const user = useAuthStore(state => state.user)
  const navigate = useNavigate()
  const client = useQueryClient()
  const [open, setOpen] = useState<Op | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const roles = user?.roles ?? []
  const uploader = roles.includes('uploader'), final = roles.includes('final_reviewer')
  const { project, open_batch: pending } = detail
  const active = project.status === 'active', voided = project.status === 'voided'
  const blocked = !!pending
  const run = (action: () => Promise<OpDetail>, target: (op: OpDetail) => string = op => `/ops/${op.id}`) => {
    setBusy(true); setError('')
    action().then(async op => { await client.invalidateQueries({ queryKey: ['project', project.id] }); setOpen(null); navigate(target(op)) })
      .catch((reason: unknown) => setError(message(reason))).finally(() => setBusy(false))
  }
  const show = (op: Op) => { setError(''); setOpen(op) }
  const form: FormProps = { detail, busy, run }
  const hasVoided = detail.products_list.some(item => item.status === 'voided') || detail.history.some(item => item.file_status === 'voided')
  if (!uploader && !final) return null
  if (project.status === 'deleted') return <p className="mt-4 text-sm text-slate-500">该项目已被物理删除，仅保留记录。</p>
  return <div className="mt-4 space-y-3">
    {pending && <p data-testid="open-batch" className="rounded-xl bg-amber-100/60 px-3 py-2 text-sm text-amber-900">该项目有一个未结束的「{pending.kind_label}」操作（{pending.batch_no} · {pending.status}），结束前不能发起新的操作。
      <Link to={`/ops/${pending.id}`} className="ml-2 underline underline-offset-4">查看</Link></p>}
    <div className="flex flex-wrap items-center gap-2" aria-label="入库后操作">
      {active && uploader && <>
        {blocked ? <GlassButton primary={false} disabled>补充</GlassButton> : <Link to="/upload" title="重新上传同名的项目文件夹，系统会自动识别为补充"><GlassButton primary={false} tabIndex={-1}>补充</GlassButton></Link>}
        <GlassButton primary={false} disabled={blocked} onClick={() => show('rename')}>改名</GlassButton>
        <GlassButton primary={false} disabled={blocked || busy} onClick={() => run(() => retagProject(project.id), op => `/batches/${op.id}/review`)}>修改标签</GlassButton>
        <GlassButton primary={false} disabled={blocked} onClick={() => show('void')}>作废</GlassButton></>}
      {final && active && <><GlassButton primary={false} disabled={blocked} onClick={() => show('move')}>移动</GlassButton>
        <GlassButton primary={false} disabled={blocked} onClick={() => show('void-project')}>整项目作废</GlassButton></>}
      {final && (voided || (active && hasVoided)) && <GlassButton primary={false} disabled={blocked} onClick={() => show('restore')}>{voided ? '恢复整项目' : '恢复'}</GlassButton>}
      {final && <GlassButton primary={false} disabled={blocked} onClick={() => show('purge')}>物理删除</GlassButton>}
    </div>
    {error && !open && <p role="alert" className="text-sm text-red-800">{error}</p>}
    <GlassModal open={open !== null} title={open ? titles[open] : ''} onClose={() => setOpen(null)}>
      {open === 'rename' && <RenameForm {...form} />}
      {open === 'void' && <VoidForm {...form} />}
      {open === 'void-project' && <VoidProjectForm {...form} />}
      {open === 'restore' && <RestoreForm {...form} />}
      {open === 'move' && <MoveForm {...form} />}
      {open === 'purge' && <PurgeForm {...form} />}
      {error && <p role="alert" className="mt-3 text-sm text-red-800">{error}</p>}
    </GlassModal>
  </div>
}
