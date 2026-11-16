import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { appendProduct, bulkUpdate, changeOwner, getReview, patchProduct, removeProduct, retryProductAi, setRelations, submitReview,
  type Pending, type ProductPatch, type ProductUpdate, type ReviewData, type ReviewField, type ReviewProduct } from '../api/review'
import { DraftSaver, type SaveStatus } from '../lib/draftSaver'
import { formatBytes, splitTerms } from '../lib/terms'
import { useAuthStore } from '../stores/auth'
import { GlassSurface } from '../components/glass/GlassSurface'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { Thumb } from '../components/review/Thumb'
import { ProductList, RoundHistory } from '../components/review/ProductList'
import { GlassModal } from '../components/glass/GlassModal'
import { cancelBatch } from '../api/upload'
import { ImageViewer } from '../components/review/ImageViewer'
import { TagForm } from '../components/review/TagForm'
import { AppendModal, BulkModal, OwnerModal, RemoveModal } from '../components/review/ReviewModals'

export function mergePatch(older: ProductPatch, newer: ProductPatch): ProductPatch {
  return { tags: { ...older.tags, ...newer.tags }, free_text: newer.free_text ?? older.free_text,
    confirm_fields: [...new Set([...(older.confirm_fields ?? []), ...(newer.confirm_fields ?? [])])],
    resolve_flags: [...new Set([...(older.resolve_flags ?? []), ...(newer.resolve_flags ?? [])])] }
}
/** 把尚未保存的修改先显示在界面上；计数等以服务端返回为准。 */
export function applyPatch(product: ReviewProduct, patch: ProductPatch): ReviewProduct {
  const tags = { ...product.tags }
  for (const [key, value] of Object.entries(patch.tags ?? {})) {
    const old = tags[key]
    if (old ? old.value === value : !value.trim()) continue
    tags[key] = { value, terms: splitTerms(value), source: 'manual', ai_value: old?.ai_value ?? '', flag: null }
  }
  for (const key of patch.confirm_fields ?? []) if (tags[key] && tags[key].source !== 'manual') tags[key] = { ...tags[key], source: 'ai_confirmed', flag: null }
  const text = patch.free_text
  return { ...product, tags, flags: product.flags.filter(flag => !(patch.resolve_flags ?? []).includes(flag)),
    ...(text !== undefined && text !== product.free_text ? { free_text: text, free_text_source: 'manual' } : {}) }
}
const message = (reason: unknown) => reason instanceof Error ? reason.message : '操作未完成，请重试'
const saveLabels: Record<SaveStatus, string> = { saved: '所有修改已保存', dirty: '有未保存的修改…', saving: '正在保存…', error: '保存失败' }

function pendingText(pending: Pending): string[] {
  return [pending.yellow ? `黄色 ${pending.yellow} 项` : '', pending.relations_unconfirmed ? `套装关系未确认 ${pending.relations_unconfirmed} 个` : '',
    pending.failed ? `打标失败 ${pending.failed} 张` : '', pending.processing ? `正在打标 ${pending.processing} 张` : ''].filter(Boolean)
}
type Modal = 'bulk' | 'append' | 'remove' | 'owner' | 'withdraw' | null

export function ReviewPage() {
  const id = Number(useParams().id)
  const valid = Number.isInteger(id) && id > 0
  const user = useAuthStore(state => state.user)
  const client = useQueryClient()
  const navigate = useNavigate()
  const key = useMemo(() => ['review', id], [id])
  const [save, setSave] = useState<{ state: SaveStatus; message?: string }>({ state: 'saved' })
  const [current, setCurrent] = useState<number | null>(null)
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [drafts, setDrafts] = useState<Record<number, number[]>>({})
  const [sideBySide, setSideBySide] = useState(false)
  const [modal, setModal] = useState<Modal>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState(() => { try { return localStorage.getItem(`review-note-${id}`) ?? '' } catch { return '' } })
  // 每次服务端确认一次写入就加一，用来丢弃在写入之前发出的旧查询结果。
  const writes = useRef(0)
  const saver = useMemo(() => new DraftSaver<ProductPatch, ProductUpdate>({ delay: 600, merge: mergePatch,
    send: (productId, patch) => patchProduct(id, productId, patch),
    saved: (productId, result, newer) => {
      writes.current++
      client.setQueryData<ReviewData>(key, data => data && { ...data, pending: result.pending,
        products: data.products.map(product => product.id === productId ? newer ? applyPatch(result.product, newer) : result.product : product) })
    },
    status: (state, text) => setSave({ state, message: text }) }), [id, client, key])
  const review = useQuery({ queryKey: key, enabled: valid, retry: false,
    queryFn: async () => {
      let data: ReviewData
      let mark: number
      do { mark = writes.current; data = await getReview(id) } while (mark !== writes.current)
      return { ...data, products: data.products.map(product => { const waiting = saver.pending(product.id); return waiting ? applyPatch(product, waiting) : product }) }
    },
    refetchInterval: result => result.state.data?.pending.processing ? 1500 : false })
  useEffect(() => {
    // 离开页面时把还没发出的草稿用 keepalive 请求送出，避免丢失最后几百毫秒的输入。
    const leave = () => { for (const [productId, patch] of saver.drain()) void patchProduct(id, productId, patch, true).catch(() => undefined) }
    window.addEventListener('pagehide', leave)
    return () => { window.removeEventListener('pagehide', leave); leave(); saver.dispose() }
  }, [saver, id])
  useEffect(() => { try { if (note) localStorage.setItem(`review-note-${id}`, note); else localStorage.removeItem(`review-note-${id}`) } catch { /* 存储不可用时备注只保留在本页。 */ } }, [note, id])

  const data = review.data
  const products = data?.products ?? []
  const selected = products.find(product => product.id === current) ?? products[0]
  const items = useMemo(() => products.filter(product => product.kind === 'item'), [products])

  if (!valid) return <GlassPanel><p role="alert">批次编号不正确</p></GlassPanel>
  if (review.isPending) return <GlassPanel><p>正在加载核对页…</p></GlassPanel>
  if (review.isError || !data) return <GlassPanel><p role="alert" className="text-red-800">{review.error?.message ?? '核对页加载失败'}</p><Link to="/" className="mt-4 inline-block text-sm text-emerald-800 underline underline-offset-4">返回首页</Link></GlassPanel>

  const { batch, fields, pending } = data
  const editable = batch.editable
  const todo = pendingText(pending)
  const change = (productId: number) => (patch: ProductPatch, immediate = false) => {
    client.setQueryData<ReviewData>(key, value => value && { ...value, products: value.products.map(product => product.id === productId ? applyPatch(product, patch) : product) })
    saver.change(productId, patch)
    if (immediate) void saver.flush()
  }
  // 整页类操作先把草稿存完，再用服务端返回的整页数据替换。
  const run = async (action: () => Promise<ReviewData>, done?: (result: ReviewData) => void) => {
    setBusy(true); setError('')
    try {
      if (!(await saver.flush())) throw new Error('还有修改没有保存成功，请先重试保存')
      const result = await action()
      writes.current++
      client.setQueryData(key, result)
      done?.(result)
    } catch (reason: unknown) { setError(message(reason)) } finally { setBusy(false) }
  }
  const runProduct = async (action: () => Promise<ProductUpdate>, done?: () => void) => {
    setBusy(true); setError('')
    try {
      if (!(await saver.flush())) throw new Error('还有修改没有保存成功，请先重试保存')
      const result = await action()
      writes.current++
      client.setQueryData<ReviewData>(key, value => value && { ...value, pending: result.pending, products: value.products.map(product => product.id === result.product.id ? result.product : product) })
      done?.()
    } catch (reason: unknown) { setError(message(reason)) } finally { setBusy(false) }
  }
  const close = () => { setModal(null); setError('') }
  const draft = selected ? drafts[selected.id] ?? selected.relation_items.map(item => item.batch_product_id) : []
  const toggle = (productId: number, on: boolean) => setChecked(value => { const next = new Set(value); if (on) next.add(productId); else next.delete(productId); return next })
  const selection = products.filter(product => checked.has(product.id)).map(product => product.id)
  // 最近一次驳回（本轮）：横幅、列表标记与大图钉点都来自它。
  const history = data.history ?? []
  const rejection = batch.status === '已驳回' ? history.find(item => item.decision === 'reject') : undefined
  const rejectPins = rejection?.pins ?? []
  const pinCounts = rejectPins.reduce<Record<number, number>>((counts, pin) => ({ ...counts, [pin.batch_product_id]: (counts[pin.batch_product_id] ?? 0) + 1 }), {})
  const isUploader = user?.id === batch.uploader_id
  return <div className="space-y-4">
    {batch.status === '已驳回' && <div data-testid="reject-banner" role="status" className="rounded-2xl border border-red-300/70 bg-red-100/70 px-5 py-3 text-sm text-red-900">
      <div className="flex items-start justify-between gap-4"><div className="min-w-0">
        <p className="font-medium">本批次已被驳回（第 {batch.round} 轮）{rejection ? ` · ${rejection.stage === 'final' ? '终审' : '初审'} ${rejection.reviewer_name}` : ''}。请按意见修改后重新提交，将从初审重新开始。</p>
        {rejection?.comment && <p className="mt-1 whitespace-pre-wrap">总体意见：{rejection.comment}</p>}
        {!!rejectPins.length && <p className="mt-1 text-xs">有 {rejectPins.length} 条意见钉在图片上，列表中带「钉」标记的图选中后可在大图上查看。</p>}</div>
        {isUploader && <button type="button" disabled={busy} className="shrink-0 text-xs underline underline-offset-4 disabled:opacity-50" onClick={() => setModal('withdraw')}>撤回批次</button>}</div>
      {history.length > 1 && <details className="mt-2"><summary className="cursor-pointer text-xs">查看历次意见（{history.length}）</summary><div className="mt-2"><RoundHistory history={history} /></div></details>}</div>}
    <GlassSurface radius={24} className="px-6 py-4">
      <div className="flex items-start justify-between gap-6">
        <div className="min-w-0"><h1 className="truncate text-lg font-semibold">{batch.project_code} {batch.company_name} {batch.project_name}
          <span className="ml-3 text-sm font-normal text-slate-600">· 位置 {batch.recommended_path || '位置待定'}</span></h1>
          <p className="mt-1.5 text-xs text-slate-500">{batch.batch_no} · {batch.status} · 责任人 {batch.owner_name}
            {user?.roles.includes('admin') && <button type="button" className="ml-2 text-emerald-800 underline underline-offset-4" onClick={() => setModal('owner')}>更换责任人</button>}
            <Link to={`/batches/${batch.id}`} className="ml-3 text-emerald-800 underline underline-offset-4">批次详情</Link></p>
          <p data-testid="pending" className={`mt-2 text-sm ${todo.length ? 'text-amber-900' : 'text-emerald-800'}`}>{todo.length ? `待处理：${todo.join(' · ')}` : '待处理项已清零，可以提交审核。'}</p>
          {!editable && <p className="mt-1 text-xs text-slate-500">{['待核对', '已驳回'].includes(batch.status) ? '只有批次责任人和管理员可以修改，当前为只读。' : `批次状态为「${batch.status}」，当前为只读。`}</p>}
        </div>
        {editable && <div className="flex w-80 shrink-0 flex-col items-end gap-2">
          <textarea aria-label="给审核的备注" rows={2} maxLength={2000} className="field !rounded-xl !px-3 !py-2 text-sm" placeholder="给审核的备注（可不填；对存放位置有异议写在这里）" value={note} onChange={event => setNote(event.target.value)} />
          <div className="flex items-center gap-3">
            <span role="status" data-testid="save-status" className={`text-xs ${save.state === 'error' ? 'text-red-800' : 'text-slate-500'}`}>{saveLabels[save.state]}{save.state === 'error' && save.message ? `：${save.message}` : ''}
              {save.state === 'error' && <button type="button" className="ml-2 underline underline-offset-4" onClick={() => void saver.flush()}>重试</button>}</span>
            <GlassButton disabled={busy || !pending.can_submit || save.state === 'error'} title={todo.length ? `提交前还需处理：${todo.join('、')}` : undefined}
              onClick={() => void run(() => submitReview(id, note), () => { setNote(''); void client.invalidateQueries({ queryKey: ['batches'] }); void client.invalidateQueries({ queryKey: ['batch', id] }) })}>提交审核</GlassButton></div>
        </div>}
      </div>
      {error && !modal && <p role="alert" className="mt-2 text-sm text-red-800">{error}</p>}
    </GlassSurface>

    {/* 三栏内部各自滚动：用次级玻璃，lite 模式下不挂实时滤镜，60 张以上主图滚动才流畅。 */}
    <div className="grid h-[calc(100vh-345px)] min-h-[560px] grid-cols-[270px_minmax(0,1fr)_400px] gap-4">
      <GlassSurface radius={24} priority="secondary" className="glass-fill min-h-0 p-3"><div className="flex h-full flex-col">
        <div data-testid="product-list" className="min-h-0 flex-1 overflow-y-auto pr-1"><ProductList batchId={batch.id} products={products} fields={fields} selectedId={selected?.id} onSelect={setCurrent} checked={checked} onToggle={toggle} pinCounts={pinCounts} /></div>
        {editable && <div className="mt-2 border-t border-slate-400/20 pt-2">
          <div className="mb-2 flex items-center justify-between text-xs text-slate-500"><span>已选 {selection.length} 张</span>
            <span><button type="button" className="underline underline-offset-4" onClick={() => setChecked(new Set(items.map(item => item.id)))}>全选单品</button>
              <button type="button" className="ml-3 underline underline-offset-4" onClick={() => setChecked(new Set())}>清空</button></span></div>
          <div className="flex gap-2"><GlassButton primary={false} disabled={busy || !selection.length} onClick={() => setModal('bulk')}>批量修改</GlassButton>
            <GlassButton primary={false} disabled={busy} onClick={() => setModal('append')}>追加主图</GlassButton></div></div>}
      </div></GlassSurface>

      <GlassSurface radius={24} priority="secondary" className="glass-fill min-h-0 p-3">{selected ? <div className="flex h-full flex-col">
        <div className="mb-2 flex items-center justify-between gap-3 text-sm"><span className="truncate font-medium">{selected.display_name}</span>
          {selected.kind === 'set' && <label className="flex shrink-0 items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={sideBySide} onChange={event => setSideBySide(event.target.checked)} />并排显示包含的单品</label>}</div>
        <div className={`min-h-0 flex-1 ${selected.kind === 'set' && sideBySide ? 'grid grid-cols-2 gap-3' : ''}`}>
          <ImageViewer key={selected.id} batchId={batch.id} fileId={selected.batch_file_id} version={selected.fingerprint ?? ''} name={selected.display_name}
            pins={rejectPins.filter(pin => pin.batch_product_id === selected.id)} />
          {selected.kind === 'set' && sideBySide && <div data-testid="side-items" className="min-h-0 overflow-y-auto rounded-2xl bg-white/20 p-2">
            <div className="grid grid-cols-2 gap-2">{items.filter(item => draft.includes(item.id)).map(item => <button type="button" key={item.id} className="rounded-xl bg-white/30 p-1.5 text-left hover:bg-white/50" onClick={() => setCurrent(item.id)}>
              <Thumb batchId={batch.id} fileId={item.batch_file_id} version={item.fingerprint ?? ''} size={1024} alt={`${item.display_name} 图片`} className="aspect-square w-full" />
              <span className="mt-1 block truncate text-xs">{item.display_name}</span></button>)}</div>
            {!draft.length && <p className="p-4 text-sm text-slate-500">还没有勾选包含的单品。</p>}</div>}
        </div></div> : <p className="p-6 text-sm text-slate-500">本批次没有主图。可以在左侧追加。</p>}</GlassSurface>

      <GlassSurface radius={24} priority="secondary" className="glass-fill min-h-0 p-3"><div data-testid="tag-form" className="h-full overflow-y-auto pr-1">{selected ?
        <TagForm batchId={batch.id} product={selected} fields={fields} items={items} editable={editable} busy={busy} onChange={change(selected.id)}
          onRetry={() => void runProduct(() => retryProductAi(id, selected.id), () => void client.invalidateQueries({ queryKey: key }))} onRemove={() => setModal('remove')}
          relationDraft={draft} onRelationDraft={ids => setDrafts(value => ({ ...value, [selected.id]: ids }))}
          onConfirmRelations={() => void runProduct(() => setRelations(id, selected.id, draft), () => setDrafts(value => { const next = { ...value }; delete next[selected.id]; return next }))} /> :
        <p className="p-3 text-sm text-slate-500">选择左侧主图后在这里核对标签。</p>}</div></GlassSurface>
    </div>

    <GlassSurface radius={20} className="px-5 py-3"><details data-testid="folders"><summary className="cursor-pointer select-none text-sm font-medium">资料夹文件清单（{data.folders.reduce((sum, folder) => sum + folder.files.length, 0)} 个文件，核对阶段不能增减）</summary>
      <div className="mt-3 grid grid-cols-5 gap-4">{data.folders.map(folder => <section key={folder.name} className="min-w-0"><h3 className="mb-2 text-xs font-semibold text-slate-600">{folder.name}（{folder.files.length}）</h3>
        <ul className="max-h-56 space-y-1 overflow-y-auto text-xs">{folder.files.map(file => <li key={file.name} className="flex justify-between gap-2"><span className="truncate" title={file.name}>{file.name}</span><span className="shrink-0 text-slate-500">{formatBytes(file.size)}</span></li>)}
          {!folder.files.length && <li className="text-slate-400">空</li>}</ul></section>)}</div></details></GlassSurface>

    {modal === 'bulk' && <BulkModal fields={fields} count={selection.length} busy={busy} error={error} onClose={close}
      onApply={tags => void run(() => bulkUpdate(id, selection, tags), () => setModal(null))} />}
    {modal === 'append' && <AppendModal busy={busy} error={error} onClose={close}
      onAppend={(kind, file, name) => void run(() => appendProduct(id, kind, file, name), result => { setModal(null); setDrafts({}); const added = result.products.find(product => product.file_name === name && product.kind === kind); if (added) setCurrent(added.id) })} />}
    {modal === 'remove' && selected && <RemoveModal name={selected.display_name} isItem={selected.kind === 'item'} busy={busy} error={error} onClose={close}
      onConfirm={() => void run(() => removeProduct(id, selected.id), () => { setModal(null); setCurrent(null); setDrafts({}); toggle(selected.id, false) })} />}
    {modal === 'withdraw' && <GlassModal open title="撤回批次" onClose={close}>
      <p className="text-sm">确定撤回吗？临时区里本批次的全部文件会被删除，批次变为「已撤回」，不能恢复。</p>
      {error && <p role="alert" className="mt-4 text-sm text-red-800">{error}</p>}
      <div className="mt-5"><GlassButton disabled={busy} onClick={() => { setBusy(true); setError(''); cancelBatch(id).then(() => { void client.invalidateQueries(); navigate('/') }).catch((reason: unknown) => setError(message(reason))).finally(() => setBusy(false)) }}>确认撤回</GlassButton></div>
    </GlassModal>}
    {modal === 'owner' && <OwnerModal ownerId={batch.owner_id} busy={busy} error={error} onClose={close}
      onSave={userId => { setBusy(true); setError(''); changeOwner(id, userId).then(() => { setModal(null); return client.invalidateQueries({ queryKey: key }) }).catch((reason: unknown) => setError(message(reason))).finally(() => setBusy(false)) }} />}
  </div>
}
