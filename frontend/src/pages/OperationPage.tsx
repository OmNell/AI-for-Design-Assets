import { useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getOperation, operationLabels, previewOperation, submitOperation, updateOperation,
  type LightOperation, type OperationData, type OperationInput, type OperationManifest, type RetagPlan } from '../api/operations'
import { getReview, type ProductPatch, type ReviewField } from '../api/review'
import type { ProductTag } from '../api/upload'
import { useAuthStore } from '../stores/auth'
import { splitTerms } from '../lib/terms'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { TerminateBatchButton } from '../components/TerminateBatchButton'
import { FieldEditor } from '../components/review/TagForm'
import { RoundHistory } from '../components/review/ProductList'
import { PathPicker } from '../components/audit/PathPicker'
import { fieldLabels, OperationDiff } from '../components/ops/OperationDiff'
import { SupplementWorkspace } from '../components/ops/SupplementWorkspace'

const linkClass = 'text-sm text-emerald-800 underline underline-offset-4'
const unaudited = ['已暂存', '待核对', '已驳回', '待初审', '待终审']
type RenamePlan = { id: number; name: string; rename_file: boolean }

function NameForm({ manifest, plan, change }: { manifest: OperationManifest; plan: OperationInput; change: (value: OperationInput) => void }) {
  const products = (plan.products ?? []) as RenamePlan[]
  return <div className="space-y-3">
    <label className="block text-sm">项目名称
      <input className="field mt-1" maxLength={200} value={plan.project_name ?? manifest.project.project_name}
        onChange={event => change({ project_name: event.target.value })} /></label>
    {manifest.products.filter(row => row.status === 'active').map(row => {
      const proposal = products.find(choice => choice.id === row.id)
      const edit = (name: string, rename_file: boolean) => change({ products: [...products.filter(choice => choice.id !== row.id),
        ...(name === row.display_name && !rename_file ? [] : [{ id: row.id, name, rename_file }])] })
      return <div key={row.id} className="space-y-2 rounded-xl bg-white/30 p-3">
        <label className="block text-sm">{row.product_code} 商品展示名
          <input className="field mt-1" maxLength={200} value={proposal?.name ?? row.display_name} onChange={event => edit(event.target.value, proposal?.rename_file ?? false)} /></label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={proposal?.rename_file ?? false}
          onChange={event => edit(proposal?.name ?? row.display_name, event.target.checked)} />主图文件随展示名改名</label>
      </div>
    })}
  </div>
}

function RetagForm({ batchId, manifest, fields, plan, initialProduct, change }: { batchId: number; manifest: OperationManifest; fields: ReviewField[]
  plan: OperationInput; initialProduct: number | null; change: (value: OperationInput) => void }) {
  const products = manifest.products.filter(row => row.status === 'active')
  const [current, setCurrent] = useState(initialProduct ?? products[0]?.id)
  const [pending, setPending] = useState<Record<number, Record<string, string>>>({})
  const proposals = (plan.products ?? []) as RetagPlan[]
  const product = products.find(row => row.id === current) ?? products[0]
  const itemRows = products.filter(row => row.kind === 'item')
  if (!product) return <p className="text-sm text-slate-500">项目里暂无可修改的商品。</p>
  const proposal = proposals.find(row => row.id === product.id)
  const old = Object.fromEntries(manifest.tags?.filter(row => row.product_id === product.id).map(row => [row.field_key, row]) ?? [])
  const linked = manifest.relations?.filter(row => row.set_product_id === product.id && row.status === 'active').map(row => row.item_product_id) ?? []
  const selected = proposal?.item_ids ?? linked
  const apply = (update: Partial<RetagPlan>) => change({ products: [...proposals.filter(row => row.id !== product.id), { ...proposal, id: product.id, ...update }] })
  function editTag(field: string, patch: ProductPatch) {
    if (!product) return
    const status = patch.confirmations?.[field]
    const staged = pending[product.id] ?? {}
    const value = patch.tags?.[field] ?? staged[field] ?? proposal?.tags?.[field]?.value ?? old[field]?.value ?? ''
    if (status) {
      setPending(values => {
        const next = { ...values[product.id] }
        delete next[field]
        return { ...values, [product.id]: next }
      })
      apply({ tags: { ...proposal?.tags, [field]: { value: status === 'confirmed_unknown' ? '' : value.trim(), confirmation_status: status } } })
    } else if (patch.tags) {
      setPending(values => ({ ...values, [product.id]: { ...values[product.id], [field]: value } }))
      change({})
    }
  }
  return <div className="space-y-4">
    <label className="block text-sm">修改商品<select className="field mt-1" value={product.id} onChange={event => setCurrent(Number(event.target.value))}>
      {products.map(row => <option key={row.id} value={row.id}>{row.product_code} {row.display_name}{proposals.some(item => item.id === row.id) ? '（已修改）' : ''}</option>)}
    </select></label>
    {Object.values(pending).some(row => Object.keys(row).length) && <p role="status" className="text-sm text-amber-900">请逐项确认刚修改的标签，预览只包含已确认的值。</p>}
    <label className="block text-sm">商品展示名<input className="field mt-1" maxLength={200} value={proposal?.name ?? product.display_name}
      onChange={event => apply({ name: event.target.value })} /></label>
    <label className="block text-sm">自由描述<textarea className="field mt-1" rows={3} maxLength={5000} value={proposal?.free_text ?? product.free_text ?? ''}
      onChange={event => apply({ free_text: event.target.value })} /></label>
    <div className="grid grid-cols-2 gap-3">{fields.map(field => {
      const base = old[field.key]
      const update = proposal?.tags?.[field.key]
      const inProgress = pending[product.id]?.[field.key]
      const value = inProgress ?? update?.value ?? base?.value ?? ''
      const tag: ProductTag = { value, terms: splitTerms(value), source: update || inProgress !== undefined ? 'manual' : base?.source ?? '', ai_value: '', flag: null,
        confirmation_status: inProgress !== undefined ? 'pending' : update?.confirmation_status ?? base?.confirmation_status ?? 'pending' }
      return <FieldEditor key={`${product.id}:${field.key}`} batchId={batchId} field={field} tag={tag} showAi={false} editable canConfirm onChange={patch => editTag(field.key, patch)} />
    })}</div>
    {product.kind === 'set' && <fieldset className="rounded-xl bg-white/35 p-3"><legend className="text-sm font-medium">套装包含的单品</legend>
      {itemRows.map(row => <label key={row.id} className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" checked={selected.includes(row.id)}
        onChange={event => apply({ item_ids: event.target.checked ? [...selected, row.id] : selected.filter(id => id !== row.id) })} />{row.product_code} {row.display_name}</label>)}
      {!selected.length && <p className="text-sm text-slate-600">无关联单品</p>}
    </fieldset>}
  </div>
}

function ScopeForm({ data, plan, change }: { data: OperationData; plan: OperationInput; change: (value: OperationInput) => void }) {
  const user = useAuthStore(state => state.user)
  const { before, kind } = data
  const projectAllowed = kind === 'purge' ? before.project.status === 'voided' : !!user?.roles.includes('final_reviewer')
  const expected = kind === 'void' ? 'active' : 'voided'
  const wholeRequired = before.project.status === 'voided' && kind !== 'void'
  const mainIds = new Set(before.products.filter(row => row.status !== 'deleted').map(row => row.file_id))
  const products = before.products.filter(row => row.status === expected)
  const files = before.files.filter(row => (row.status === expected || (kind === 'void' && row.status === 'replaced')) && !mainIds.has(row.id))
  return <div className="space-y-4">
    {projectAllowed && <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={wholeRequired} checked={!!plan.project}
      onChange={event => change({ project: event.target.checked, product_ids: [], file_ids: [] })} />整个项目</label>}
    {!plan.project && <div className="grid grid-cols-2 gap-4">
      <fieldset className="space-y-2 rounded-xl bg-white/30 p-3"><legend className="font-medium">商品</legend>
        {products.map(row => <label key={row.id} className="flex gap-2 text-sm"><input type="checkbox" checked={plan.product_ids?.includes(row.id) ?? false}
          onChange={event => change({ product_ids: event.target.checked ? [...(plan.product_ids ?? []), row.id] : plan.product_ids?.filter(id => id !== row.id) })} />{row.product_code} {row.display_name}</label>)}
        {!products.length && <p className="text-sm text-slate-500">暂无可选商品。</p>}</fieldset>
      <fieldset className="space-y-2 rounded-xl bg-white/30 p-3"><legend className="font-medium">资料及历史文件</legend>
        {files.map(row => <label key={row.id} className="flex gap-2 break-all text-sm"><input type="checkbox" checked={plan.file_ids?.includes(row.id) ?? false}
          onChange={event => change({ file_ids: event.target.checked ? [...(plan.file_ids ?? []), row.id] : plan.file_ids?.filter(id => id !== row.id) })} />{row.rel_path}</label>)}
        {!files.length && <p className="text-sm text-slate-500">暂无可选文件。</p>}</fieldset>
    </div>}
    {kind === 'void' && <p className="text-sm text-slate-600">作废套装只影响该套装及有效关联，其单品仍保持有效。</p>}
    {kind === 'restore' && plan.project && <p className="text-sm text-slate-600">默认恢复原位置。原位置无法使用时由终审选择新目录。</p>}
  </div>
}

export function OperationPage() {
  const id = Number(useParams().id)
  const valid = Number.isSafeInteger(id) && id > 0
  const data = useQuery({ queryKey: ['operation', id], queryFn: () => getOperation(id), enabled: valid, retry: false })
  if (!valid) return <GlassPanel><p role="alert">批次编号不正确</p></GlassPanel>
  if (data.isPending) return <GlassPanel><p role="status">正在读取操作草稿…</p></GlassPanel>
  if (data.isError) return <GlassPanel><p role="alert">{data.error.message}</p></GlassPanel>
  if (data.data.kind === 'supplement') return <SupplementWorkspace key={id} data={data.data} />
  return <LightDraft key={`${id}:${data.data.status}`} data={data.data} />
}

function LightDraft({ data }: { data: OperationData }) {
  const { batch_id: id, before, editable, status } = data
  const kind = data.kind as LightOperation
  const { state } = useLocation()
  const entry = state as { productId?: number; fileId?: number } | null
  const user = useAuthStore(value => value.user)
  const navigate = useNavigate()
  const cache = useQueryClient()
  const review = useQuery({ queryKey: ['review', id], queryFn: () => getReview(id), retry: false })
  const [plan, setPlan] = useState<OperationInput>(() => ({
    ...(before.project.status === 'voided' && ['restore', 'purge'].includes(kind) ? { project: true } :
      ['void', 'restore', 'purge'].includes(kind) ? { product_ids: entry?.productId ? [entry.productId] : [], file_ids: entry?.fileId ? [entry.fileId] : [] } : {}),
    ...data.params,
  }))
  const [preview, setPreview] = useState<OperationData | null>(null)
  const [description, setDescription] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [showPath, setShowPath] = useState(kind === 'move' && !plan.company_id)
  const fields = review.data?.fields ?? Object.entries(fieldLabels).map(([key, label]) => ({ key, label, hint: '' }))
  const set = (patch: OperationInput) => { setPlan(current => ({ ...current, ...patch })); setPreview(null); setCode('') }
  const run = async (action: () => Promise<void>) => {
    if (busy) return
    setBusy(true); setError('')
    try { await action() } catch (reason) { setError(reason instanceof Error ? reason.message : '操作未完成，请重试') }
    finally { setBusy(false) }
  }
  const save = async () => {
    const draft = await updateOperation(id, plan)
    cache.setQueryData(['operation', id], draft)
    setPreview(await previewOperation(id))
  }
  const submit = async () => {
    const result = await submitOperation(id, { ...plan, ...(kind === 'purge' ? { confirm: code.trim() } : {}) }, description)
    cache.setQueryData(['review', id], result)
    await Promise.all([['batches'], ['batch', id], ['project-operations', data.project_id], ['operation', id], ['todos']].map(queryKey => cache.invalidateQueries({ queryKey })))
    navigate(`/batches/${id}`)
  }
  const ready = kind === 'move' ? !!plan.company_id && !!plan.reason?.trim() : kind === 'rename' ? !!plan.project_name?.trim() || !!plan.products?.length
    : kind === 'retag' ? !!plan.products?.length : !!plan.reason?.trim() && !!(plan.project || plan.product_ids?.length || plan.file_ids?.length)
  return <div className="space-y-5">
    <GlassPanel><div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div><Link to={`/projects/${data.project_id}/operations`} className={linkClass}>返回资产操作</Link>
          <h1 className="mt-3 text-2xl font-semibold">{kind === 'purge' ? '申请物理删除' : operationLabels[kind]}</h1>
          <p className="mt-2 text-sm text-slate-600">{before.project.project_code} · {before.project.project_name} · {status}</p></div>
        {unaudited.includes(status) && review.data?.batch.uploader_id === user?.id && <TerminateBatchButton id={id} disabled={busy} onTerminated={() => {
          void cache.invalidateQueries({ queryKey: ['batches'] })
          navigate(`/batches/${id}`)
        }} />}
      </div>
      {!!review.data?.history?.length && <RoundHistory history={review.data.history} />}
      {editable && <>
        {kind === 'rename' && <NameForm manifest={before} plan={plan} change={set} />}
        {kind === 'retag' && <RetagForm batchId={id} manifest={before} plan={plan} initialProduct={entry?.productId ?? null} change={set} fields={fields} />}
        {['void', 'restore', 'purge'].includes(kind) && <ScopeForm data={data} plan={plan} change={set} />}
        {kind === 'move' && <>
          <label className="text-sm">当前目录　{before.project.current_path}</label>
          {showPath ? <PathPicker won={plan.won ?? before.project.won} busy={busy}
            onSave={(won, company_id) => { set({ won, company_id }); setShowPath(false) }} onCancel={() => setShowPath(false)} />
            : <p className="text-sm">{plan.company_id && (plan.won ? '已中标' : '未中标')} <button type="button" className={linkClass} onClick={() => setShowPath(true)}>选择目标目录</button></p>}
        </>}
        {['void', 'restore', 'purge', 'move'].includes(kind) && <label className="block text-sm">操作原因
          <textarea className="field mt-1" rows={3} maxLength={500} value={plan.reason ?? ''} onChange={event => set({ reason: event.target.value })} /></label>}
        <GlassButton primary={false} disabled={!ready || busy} onClick={() => void run(save)}>保存草稿并预览</GlassButton>
      </>}
      {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    </div></GlassPanel>
    {(preview || !editable && (data.after || data.purge)) && <OperationDiff operation={preview ?? data} />}
    {editable && preview && <GlassPanel><div className="space-y-3">
      <label className="block text-sm">给审核的备注<textarea rows={2} className="field mt-1" maxLength={2000} value={description} onChange={event => setDescription(event.target.value)} /></label>
      {kind === 'purge' && preview.purge && (preview.purge.eligible
        ? <label className="block text-sm">输入编号 <strong>{preview.purge.confirm_label}</strong> 确认提交
          <input className="field mt-1 max-w-sm" value={code} onChange={event => setCode(event.target.value)} autoComplete="off" /></label>
        : <p className="text-sm text-amber-900">冷静期尚未结束。最早可申请时间为 {new Date(preview.purge.earliest_allowed).toLocaleString('zh-CN')}。</p>)}
      <GlassButton disabled={busy || kind === 'purge' && (!preview.purge?.eligible || code.trim() !== preview.purge.confirm_label)}
        onClick={() => void run(submit)}>提交初审</GlassButton>
      {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    </div></GlassPanel>}
  </div>
}
