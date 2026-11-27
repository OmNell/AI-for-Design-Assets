import { Button, Dialog, Panel } from '../components/ui'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { changePath, claimAudit, getAudit, passAudit, rejectAudit, releaseAudit, savePins, type AuditData, type PinInput } from '../api/audit'
import { OperationDiff } from '../components/ops/OperationDiff'
import { SupplementAuditComparison } from '../components/ops/SupplementComparison'
import { OperationExecution } from '../components/ops/OperationExecution'
import { useAuthStore } from '../stores/auth'
import { AssetPreview } from '../components/AssetPreview'
import { IngestPanel } from '../components/IngestPanel'
import { ImageViewer } from '../components/review/ImageViewer'
import { ProductList, RoundHistory } from '../components/review/ProductList'
import { TagForm } from '../components/review/TagForm'
import { Thumb } from '../components/review/Thumb'
import { PathPicker } from '../components/audit/PathPicker'

const message = (reason: unknown) => reason instanceof Error ? reason.message : '操作未完成，请重试'
const stageNames = { first: '初审', final: '终审' }
const Row = ({ label, children }: { label: string; children: React.ReactNode }) => <div className="grid grid-cols-[76px_minmax(0,1fr)] gap-2 border-b border-slate-400/15 py-2.5 text-sm"><dt className="text-slate-500">{label}</dt><dd className="min-w-0">{children}</dd></div>

export function AuditPage() {
  const id = Number(useParams().id)
  const valid = Number.isInteger(id) && id > 0
  const user = useAuthStore(state => state.user)
  const client = useQueryClient()
  const key = useMemo(() => ['audit', id], [id])
  const audit = useQuery({ queryKey: key, queryFn: () => getAudit(id), enabled: valid, retry: false, refetchInterval: 30000 })
  const [current, setCurrent] = useState<number | null>(null)
  const [tab, setTab] = useState<'info' | 'tags'>('info')
  const [sideBySide, setSideBySide] = useState(false)
  const [compareOriginal, setCompareOriginal] = useState(true)
  const [checks, setChecks] = useState<Record<string, boolean>>({})
  const [pins, setPins] = useState<PinInput[]>([])
  const [editingPath, setEditingPath] = useState(false)
  const [rejecting, setRejecting] = useState(false)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState('')
  const pinQueue = useRef(Promise.resolve())
  const data = audit.data
  // 服务端的暂存钉点只在首次加载和换批次时灌入本地，之后以本地编辑为准。
  const loaded = data ? `${data.review.batch.id}:${data.review.batch.round}:${data.stage}:${data.claimed_by_me}:${data.version}` : ''
  useEffect(() => { setChecks({}); setRejecting(false); setComment(''); setEditingPath(false); if (data) setPins(data.pins.map(pin => ({ batch_product_id: pin.batch_product_id, x: pin.x, y: pin.y, text: pin.text }))) }, [loaded])

  if (!valid) return <Panel><p role="alert">批次编号不正确</p></Panel>
  if (audit.isPending) return <Panel><p>正在加载审核页…</p></Panel>
  if (audit.isError || !data) return <Panel><p role="alert" className="text-red-800">{audit.error?.message ?? '审核页加载失败'}</p><Link to="/" className="mt-4 inline-block text-sm text-blue-700 underline underline-offset-4">返回首页</Link></Panel>

  const { review, path, stage } = data
  const { batch, fields, products } = review
  const selected = products.find(product => product.id === current) ?? products[0]
  const operation = data.operation
  const assignment = operation?.kind === 'supplement' && selected
    ? operation.params.products?.find(row => row.id === selected.id && 'existing_product_id' in row)
    : undefined
  const original = assignment && 'existing_product_id' in assignment
    ? operation?.before.products.find(row => row.id === assignment.existing_product_id) : undefined
  const comparing = !!original && compareOriginal
  const comparingSet = selected?.kind === 'set' && sideBySide && !comparing
  const items = products.filter(product => product.kind === 'item')
  const mine = data.claimed_by_me
  const allChecked = Object.keys(data.checklist).every(item => checks[item])
  const pathBlocked = stage === 'final' && !path.effective && !data.operation
  const pinCounts = pins.reduce<Record<number, number>>((counts, pin) => ({ ...counts, [pin.batch_product_id]: (counts[pin.batch_product_id] ?? 0) + 1 }), {})
  const run = async (action: () => Promise<AuditData>, after?: (result: AuditData) => void) => {
    setBusy(true); setError('')
    try {
      const result = await action()
      client.setQueryData(key, result)
      void client.invalidateQueries({ queryKey: ['queue'] }); void client.invalidateQueries({ queryKey: ['todos'] })
      after?.(result)
    } catch (reason: unknown) { setError(message(reason)); setChecks({}); void audit.refetch() } finally { setBusy(false) }
  }
  const storePins = (next: PinInput[]) => { setPins(next); pinQueue.current = pinQueue.current.then(() => savePins(id, data.version, next)).then(() => undefined).catch((reason: unknown) => setError(message(reason))) }
  const selectedPins = selected ? pins.map((pin, index) => ({ ...pin, index })).filter(pin => pin.batch_product_id === selected.id) : []
  const relationDraft = selected ? selected.relation_items.map(item => item.batch_product_id) : []

  return <div className="space-y-4">
    <div className="ui-panel px-6 py-4"><div className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0"><h1 className="truncate text-lg font-semibold">{stage ? stageNames[stage] : '审核记录'} · {batch.project_code} {batch.company_name} {batch.project_name}</h1>
        <p className="mt-1.5 text-xs text-slate-500">{batch.batch_no} · {batch.status} · 第 {batch.round} 轮 · 责任人 {batch.owner_name}
          <Link to={stage === 'final' ? '/final-review' : '/first-review'} className="ml-3 text-blue-700 underline underline-offset-4">返回队列</Link></p>
        <p data-testid="claim-state" className="mt-2 text-sm">{!stage ? `批次当前为「${batch.status}」，不在待审状态，仅供查看。` : mine ? '你已领取本批次，可以审核。' :
          data.blocked ? '你已完成本轮初审，请由另一位有终审权限的用户终审。' : data.claimed_by ? `已由 ${data.claimed_by.name} 领取。` : data.can_claim ? '尚未领取。领取后才能钉意见、通过或驳回。' : '当前账号没有这一级的审核权限，仅供查看。'}</p>
        {done && <p role="status" className="mt-1 text-sm text-emerald-800">{done}</p>}
        {error && !rejecting && <p role="alert" className="mt-1 text-sm text-red-800">{error}</p>}</div>
      <div className="flex shrink-0 flex-wrap gap-2" data-testid="review-actions">{data.can_claim && <Button disabled={busy} onClick={() => void run(() => claimAudit(id, data.version))}>领取</Button>}
        {stage && data.claimed_by && (mine || user?.roles.includes('admin')) && <Button primary={false} disabled={busy} onClick={() => void run(() => releaseAudit(id, data.version))}>释放领取</Button>}
        {stage && <Button primary={false} className="!border-red-200 !text-red-800" disabled={busy || !mine}
          title={!mine ? '只有本轮领取人可以驳回' : undefined}
          onClick={() => { setError(''); setRejecting(true) }}>驳回</Button>}
      </div>
    </div></div>

    {['入库中', '入库异常', '已入库'].includes(batch.status) && (data.operation
      ? <OperationExecution batchId={id} projectId={data.operation.before.project.id} kind={data.operation.kind} />
      : <IngestPanel batchId={id} />)}
    {data.supplement && <SupplementAuditComparison comparison={data.supplement} batchId={batch.id} version={batch.review_snapshot_hash} />}
    {data.operation && <OperationDiff operation={data.operation} />}

    <div className={data.operation && !products.length ? "grid grid-cols-1" : "editor-layout review-layout"}>
      {(!data.operation || products.length > 0) && <div className="ui-panel min-h-0 min-w-0 overflow-hidden p-3"><div data-testid="product-list" className="editor-list h-full overflow-y-auto pr-1">
        <ProductList batchId={batch.id} products={products} fields={fields} selectedId={selected?.id} onSelect={setCurrent} pinCounts={pinCounts} /></div></div>}

      {(!data.operation || products.length > 0) && <div className="ui-panel min-h-0 min-w-0 overflow-hidden p-3">{selected ? <div className="flex h-full flex-col">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-sm"><span className="truncate font-medium">{selected.display_name}</span>
          {original && <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={compareOriginal}
            onChange={event => setCompareOriginal(event.target.checked)} />对比原主图</label>}
          {selected.kind === 'set' && !comparing && <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={sideBySide} onChange={event => setSideBySide(event.target.checked)} />并排显示包含的单品</label>}</div>
        <div className={`review-preview min-h-0 flex-1 ${comparing || comparingSet ? 'grid grid-cols-2 gap-3' : ''}`}>
          {comparing && original && <section className="min-h-0 min-w-0 overflow-y-auto rounded-2xl bg-slate-50 p-2" aria-label="原主图">
            <h3 className="mb-2 break-words text-xs font-medium">原主图 · {original.product_code}</h3>
            <AssetPreview key={original.file_id} fileId={original.file_id} name={original.display_name} verifiedOnly compact />
          </section>}
          <div className="flex min-h-0 min-w-0 flex-col">
            {comparing && <h3 className="mb-2 break-words p-2 text-xs font-medium">待审新主图 · {selected.display_name}</h3>}
            <div className="min-h-0 flex-1"><ImageViewer key={selected.id} batchId={batch.id} fileId={selected.batch_file_id} version={selected.fingerprint ?? ''} name={selected.display_name} pins={selectedPins}
            onPin={mine ? (x, y) => { storePins([...pins, { batch_product_id: selected.id, x, y, text: '' }]); setTab('info') } : undefined} /></div>
          </div>
          {comparingSet && <div className="min-h-0 overflow-y-auto rounded-2xl bg-slate-50 p-2"><div className="grid grid-cols-2 gap-2">{items.filter(item => relationDraft.includes(item.id)).map(item =>
            <button type="button" key={item.id} className="rounded-xl bg-slate-50 p-1.5 text-left hover:bg-slate-50" onClick={() => setCurrent(item.id)}>
              <Thumb batchId={batch.id} fileId={item.batch_file_id} version={item.fingerprint ?? ''} size={1024} alt={`${item.display_name} 图片`} className="aspect-square w-full" />
              <span className="mt-1 block truncate text-xs">{item.display_name}</span></button>)}</div></div>}
        </div></div> : <p className="p-6 text-sm text-slate-500">本批次没有主图。</p>}</div>}

      <div className="ui-panel min-h-0 min-w-0 overflow-hidden p-3"><div className="flex h-full flex-col">
        <div role="tablist" className="mb-2 flex gap-1 rounded-lg bg-slate-100 p-1 text-sm">{([['info', '审核信息'], ['tags', '标签（只读）']] as const).map(([value, label]) =>
          <button key={value} type="button" role="tab" aria-selected={tab === value} className={`flex-1 rounded-md py-1.5 ${tab === value ? 'bg-white font-medium' : 'text-slate-600'}`} onClick={() => setTab(value)}>{label}</button>)}</div>
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">{tab === 'tags' ? selected ?
          <TagForm batchId={batch.id} product={selected} fields={fields} items={items} editable={false} busy onChange={() => undefined} onRetry={() => undefined} onRemove={() => undefined}
            relationDraft={relationDraft} onRelationDraft={() => undefined} onConfirmRelations={() => undefined} /> : <p className="text-sm text-slate-500">没有主图。</p> :
          <div data-testid="audit-info"><dl>
            <Row label="存放位置">{path.effective ? <span className="break-all">{path.effective.split('/').join(' / ')}</span> :
              <span className="text-amber-900">位置待定——企业「{path.company_name}」{path.company_in_tree ? '已在对照表中，请终审选择位置' : '不在对照表中'}</span>}
              {path.final && path.recommended && path.final !== path.recommended && <p className="mt-1 text-xs text-slate-500">原推荐位置：{path.recommended.split('/').join(' / ')}</p>}
              {data.can_edit_path && !editingPath && <button type="button" className="ml-2 text-xs text-blue-700 underline underline-offset-4" onClick={() => setEditingPath(true)}>修改</button>}
              {data.can_edit_path && editingPath && <PathPicker won={batch.won} busy={busy} onCancel={() => setEditingPath(false)} onSave={(won, company) => void run(() => changePath(id, data.version, won, company), () => setEditingPath(false))} />}</Row>
            <Row label="预检提醒">{data.acks.length ? data.acks.map(item => <p key={item}>{item}</p>) : '无'}</Row>
            <Row label="核对记录">人工修改 {data.stats.manual_edits} 处 · 确认 AI 值 {data.stats.ai_confirmed} 处 · 明确留空 {data.stats.confirmed_unknown} 项 · 追加 {data.stats.appended} 张 · 移除 {data.stats.removed} 张</Row>
            <Row label="套装关系">{data.relations.length ? data.relations.map(item => <p key={item.set_id}>{item.set_name} → {item.items.join('、') || '（不含单品）'}
              <span className="text-xs text-slate-500">（AI 推荐 {item.ai_count}，人工补 {item.manual_count}）</span></p>) : '没有套装图'}</Row>
            <Row label="上传人备注">{data.note ? <span className="whitespace-pre-wrap">“{data.note}”</span> : '无'}</Row>
            <Row label="资料夹">{data.folder_counts.map(item => `${item.name} ${item.count} 个`).join(' · ')}</Row>
          </dl>
          {mine && <section className="mt-3"><h3 className="text-sm font-medium">钉图意见（随驳回发出，通过则作废）</h3>
            {!pins.length && <p className="mt-1 text-xs text-slate-500">在大图左下角点「钉意见」，再点图片上的位置。</p>}
            <ul className="mt-2 space-y-2">{pins.map((pin, index) => <li key={index} className="flex items-start gap-2 text-xs">
              <button type="button" className="mt-1.5 shrink-0 rounded-full bg-red-600 px-1.5 text-white" title="查看这张图" onClick={() => setCurrent(pin.batch_product_id)}>{products.find(product => product.id === pin.batch_product_id)?.display_name ?? '图'}</button>
              <input aria-label={`第 ${index + 1} 条钉图意见`} className="field !rounded-xl !px-2.5 !py-1.5 text-xs" maxLength={500} placeholder="写下这里的问题" value={pin.text}
                onChange={event => setPins(pins.map((item, position) => position === index ? { ...item, text: event.target.value } : item))} onBlur={() => storePins(pins)} />
              <button type="button" aria-label={`删除第 ${index + 1} 条钉图意见`} className="mt-1.5 shrink-0 text-slate-500 hover:text-red-700" onClick={() => storePins(pins.filter((_, position) => position !== index))}>删除</button></li>)}</ul></section>}
          <section className="mt-4"><h3 className="mb-2 text-sm font-medium">历次意见</h3><RoundHistory history={data.history} /></section>
          {mine && <section className="mt-4 rounded-xl bg-slate-50 p-3"><h3 className="text-sm font-medium">审核清单（全部勾选才能通过）</h3>
            <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2">{Object.entries(data.checklist).map(([item, label]) => <label key={item} className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={!!checks[item]} onChange={event => setChecks({ ...checks, [item]: event.target.checked })} />{label}</label>)}</div>
            {pathBlocked && <p data-testid="path-blocked" className="mt-3 text-xs text-amber-900">{path.company_in_tree ? '位置待定：请先点上方「修改」从目录树选择存放位置。' : '位置待定：等待管理员在对照表中录入该企业。'}</p>}
            <div className="mt-3 flex gap-3"><Button disabled={busy || !allChecked || pathBlocked} title={!allChecked ? '审核清单未全部勾选' : undefined}
              onClick={() => void run(() => passAudit(id, data.version, checks), () => { setChecks({}); setDone(stage === 'final' ? '终审已通过，已安排入库。' : '初审已通过，批次进入「待终审」。') })}>通过</Button>
            </div></section>}
          </div>}</div>
      </div></div>
    </div>

    <Dialog open={rejecting} title="驳回批次" onClose={() => setRejecting(false)}>
      <p className="text-sm text-slate-500">整包驳回，退回上传人；修改后重新提交会从初审重新开始。{pins.length ? `将连同 ${pins.length} 条钉图意见一起发出。` : ''}</p>
      <textarea aria-label="总体意见" rows={5} maxLength={2000} className="field mt-4 text-sm" placeholder="总体意见（必填）" value={comment} onChange={event => setComment(event.target.value)} />
      {error && <p role="alert" className="mt-3 text-sm text-red-800">{error}</p>}
      <div className="mt-5"><Button disabled={busy || !mine || !comment.trim()} onClick={() => void run(async () => { await pinQueue.current; return rejectAudit(id, data.version, comment, pins) }, () => { setRejecting(false); setComment(''); setDone('已驳回，批次退回上传人。') })}>确认驳回</Button></div>
    </Dialog>
  </div>
}
