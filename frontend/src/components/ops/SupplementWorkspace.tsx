import { Button, Panel } from '../ui'
import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { updateOperation, type OperationData, type OperationInput, type SupplementFile } from '../../api/operations'
import { confirmBatch, getBatch, getPreDescriptions } from '../../api/upload'
import type { ReviewProduct } from '../../api/review'
import { formatBytes } from '../../lib/terms'
import { TerminateBatchButton } from '../TerminateBatchButton'
import { Thumb } from '../review/Thumb'
import { AssetPreview } from '../AssetPreview'
import { SupplementPictures } from './SupplementComparison'

const conflictNames = { new: '新增', duplicate: '相同，跳过', conflict: '同路径不同内容' }
const choiceNames = { replace: '替换原文件', keep_both: '另存为新文件（自动命名）' }
const linkClass = 'text-sm text-blue-700 underline underline-offset-4'
type ConflictChoice = NonNullable<SupplementFile['choice']>
const toggle = (values: number[], id: number, checked: boolean) => checked ? [...values, id] : values.filter(value => value !== id)

function ConflictFiles({ data, choices, onChoice, replacedMains = new Set<number>(), version }: {
  data: OperationData; choices: Record<number, ConflictChoice>; replacedMains?: Set<number>; version?: string | null
  onChoice?: (id: number, choice: ConflictChoice | '') => void
}) {
  const oldFiles = new Map(data.before.files.map(file => [file.id, file]))
  const replaced = data.conflicts.filter(row => row.conflict !== 'duplicate' &&
    (choices[row.id] === 'replace' || replacedMains.has(row.id))).length
  const keptBoth = data.conflicts.filter(row => choices[row.id] === 'keep_both').length
  const added = data.conflicts.filter(row => row.conflict === 'new' && !replacedMains.has(row.id)).length + keptBoth
  const rows = data.status === '已暂存' ? data.conflicts : data.conflicts.filter(row => row.conflict === 'conflict')
  return <div className="space-y-3">
    <p className="text-sm text-slate-600">新增 {added} · 替换 {replaced} · 跳过 {data.conflicts.filter(row => row.conflict === 'duplicate').length}
      {keptBoth > 0 && `（新增含另存 ${keptBoth}）`}</p>
    {rows.map(row => {
      const old = row.file_id ? oldFiles.get(row.file_id) : undefined
      const mainImage = /^(套装|单品)\/[^/]+\.(jpe?g|png)$/i.test(row.rel_path)
      return <section key={row.id} className="rounded-xl bg-slate-50 p-3">
        <h3 className="break-all text-sm font-medium">{row.rel_path} · {row.conflict ? conflictNames[row.conflict] : '尚未核对'}</h3>
        {row.conflict === 'conflict' && <div className="mt-3 grid grid-cols-2 gap-3 text-xs text-slate-600">
          <div className="min-w-0"><p>当前文件 {old ? formatBytes(old.size) : '—'}{old?.mtime && ` · ${new Date(old.mtime).toLocaleString('zh-CN')}`}</p>
            <p className="mt-1 break-all">SHA-256 {old?.fingerprint || '—'}</p>
            {old && <Link to={`/files/${old.id}`} className={linkClass}>打开当前文件</Link>}
            {mainImage && old && <details className="mt-2"><summary className="cursor-pointer">预览当前主图</summary><AssetPreview fileId={old.id} name={old.rel_path} /></details>}</div>
          <div className="min-w-0"><p>上传文件 {row.size !== undefined ? formatBytes(row.size) : '—'}{row.mtime !== undefined && ` · ${new Date(row.mtime).toLocaleString('zh-CN')}`}</p>
            <p className="mt-1 break-all">SHA-256 {row.fingerprint || '—'}</p>
            {mainImage && <Thumb batchId={data.batch_id} fileId={row.id} version={row.fingerprint ?? ''} size={300} alt={`新主图 ${row.rel_path}`} className="mt-2 max-h-36 max-w-48" />}</div>
        </div>}
        {row.conflict === 'conflict' && old && !mainImage && /\.(jpe?g|png)$/i.test(row.rel_path)
          && (!version || choices[row.id] === 'replace') &&
          <SupplementPictures key={version ?? 'draft'} batchId={data.batch_id} version={version} original={old} uploaded={row} />}
        {row.conflict === 'conflict' && (onChoice ? <label className="mt-3 block text-sm">冲突处理
          <select className="field mt-1" value={choices[row.id] ?? ''} onChange={event => onChoice(row.id, event.target.value as ConflictChoice | '')}>
            <option value="">请选择</option>{Object.entries(choiceNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
        </label> : <p className="mt-2 text-sm text-slate-600">{choiceNames[choices[row.id]] ?? '未选择处理方式'}</p>)}
      </section>
    })}
  </div>
}

export function SupplementWorkspace({ data }: { data: OperationData }) {
  const id = data.batch_id
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const cache = useQueryClient()
  const batch = useQuery({ queryKey: ['batch', id], queryFn: () => getBatch(id), retry: false })
  const [choices, setChoices] = useState<Record<number, ConflictChoice>>(() => Object.fromEntries(data.conflicts.flatMap(row => row.choice ? [[row.id, row.choice]] : [])))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const ready = !!data.conflicts.length && data.conflicts.every(row => row.conflict && (row.conflict !== 'conflict' || choices[row.id]))
  const replace = Number(params.get('replace'))
  const action = async () => {
    if (busy || !ready) return
    setBusy(true); setError('')
    try {
      await updateOperation(id, { ...data.params, files: Object.entries(choices).map(([id, choice]) => ({ id: Number(id), choice })) })
      const descriptions = await getPreDescriptions(id)
      if (descriptions.products.length) {
        cache.setQueryData(['pre-descriptions', id], descriptions)
        await cache.invalidateQueries({ queryKey: ['operation', id] })
        navigate('/batches/' + id + '/pre-descriptions' + (replace > 0 ? '?replace=' + replace : ''))
        return
      }
      const value = await confirmBatch(id)
      cache.setQueryData(['batch', id], value)
      await cache.invalidateQueries({ queryKey: ['operation', id] })
      navigate(value.status === '待核对' ? `/batches/${id}/review${replace > 0 ? `?replace=${replace}` : ''}`
        : `/batches/${id}${replace > 0 ? `?replace=${replace}` : ''}`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法提交补充文件') }
    finally { setBusy(false) }
  }
  if (data.status !== '已暂存') return <Panel><p className="mb-4 text-sm">补充批次 · {data.status}</p>
    <Link className={linkClass} to={`/batches/${id}`}>查看批次详情</Link></Panel>
  return <Panel><div className="space-y-4">
    <div className="flex items-center justify-between gap-3"><h1 className="text-2xl font-semibold">核对补充文件</h1>
      <Link className={linkClass} to={`/batches/${id}`}>返回批次详情</Link></div>
    <p className="text-sm text-slate-600">{data.before.project.project_code} · {data.before.project.project_name}</p>
    <ConflictFiles data={data} choices={choices} onChoice={data.editable ? (id, choice) => setChoices(current => {
      const values = { ...current }
      if (choice) values[id] = choice
      else delete values[id]
      return values
    }) : undefined} />
    {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    {data.editable && <div className="flex gap-2">
      <Button disabled={!ready || busy} onClick={() => void action()}>确认文件并继续核对</Button>
      {!!batch.data && <TerminateBatchButton id={id} disabled={busy} onTerminated={() => navigate(`/batches/${id}`)} />}
    </div>}
  </div></Panel>
}

export function SupplementReviewPanel({ data, products, editable, busy, preferredProduct, save, onDirty, version }: {
  data: OperationData; products: ReviewProduct[]; editable: boolean; busy: boolean; preferredProduct: number | null; version: string | null
  save: (input: OperationInput) => Promise<void>; onDirty: (dirty: boolean) => void
}) {
  const [choices, setChoices] = useState<OperationInput>(data.params)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const assignments = (choices.products ?? []) as { id: number; existing_product_id: number | null }[]
  const fileChoices = Object.fromEntries((choices.files ?? []).map(row => [row.id, row.choice]))
  const replacedMains = new Set(products.filter(product => assignments.some(row => row.id === product.id && row.existing_product_id))
    .map(product => product.batch_file_id))
  const oldProducts = data.before.products
  const oldItems = oldProducts.filter(row => row.kind === 'item' && row.status === 'active')
  const newItems = products.filter(row => row.kind === 'item' && !assignments.some(a => a.id === row.id && a.existing_product_id))
  const sets = products.filter(row => row.kind === 'set')
  const required = data.required_relation_reviews ?? []
  const choose = (patch: OperationInput) => { setChoices(current => ({ ...current, ...patch })); onDirty(true); setMessage('') }
  const apply = async () => {
    setSaving(true); setMessage('')
    try { await save(choices); onDirty(false); setMessage('补充核对已保存') }
    catch (error) { setMessage(error instanceof Error ? error.message : '无法保存补充核对') }
    finally { setSaving(false) }
  }
  const oldSetItems = (id: number) => data.before.relations?.filter(row => row.set_product_id === id && row.status === 'active').map(row => row.item_product_id) ?? []
  const confirmedRelations = (id: number) => choices.relation_reviews?.[String(id)]
  const newSetOldItems = (id: number) => choices.batch_set_items?.[String(id)]
  const oldSetNewItems = (id: number) => choices.relation_batch_items?.[String(id)] ?? []
  const updateOldSet = (id: number, items: number[]) => choose({ relation_reviews: { ...choices.relation_reviews, [id]: items } })
  return <Panel><details open><summary className="cursor-pointer text-lg font-semibold">补充文件与商品身份核对</summary>
    <div className="mt-4 space-y-4">
      <ConflictFiles data={data} choices={fileChoices} replacedMains={replacedMains} version={version} />
      {!!products.length && <p className="text-sm text-slate-600">为每张主图选择新增商品或具体的替换目标。保存替换目标后，原标签与新识别值会出现在右侧核对面板。</p>}
      {preferredProduct && <p className="text-sm text-slate-600">从「{oldProducts.find(row => row.id === preferredProduct)?.display_name ?? '商品'}」的替换入口进入，请在相应主图中手动选择商品。</p>}
      <fieldset disabled={!editable || busy || saving} className="space-y-3">
        {products.map(product => {
          const assignment = assignments.find(row => row.id === product.id)
          return <label key={product.id} className="block text-sm">{product.display_name} 的商品身份
            <select className="field mt-1" value={assignment ? assignment.existing_product_id ?? 'new' : ''} onChange={event => {
              const value = event.target.value
              choose({ products: [...assignments.filter(row => row.id !== product.id), ...(value ? [{ id: product.id, existing_product_id: value === 'new' ? null : Number(value) }] : [])] })
            }}>
              <option value="">请选择</option><option value="new">作为新增商品</option>
              {oldProducts.filter(row => row.status === 'active' && row.kind === product.kind).map(row =>
                <option key={row.id} value={row.id} disabled={assignments.some(choice => choice.id !== product.id && choice.existing_product_id === row.id)}>{row.id === preferredProduct ? '入口商品 · ' : ''}替换 {row.product_code} {row.display_name}</option>)}
            </select>
          </label>
        })}
        {sets.map(set => {
          const previousId = assignments.find(row => row.id === set.id)?.existing_product_id
          const proposed = newSetOldItems(set.id) ?? (previousId ? oldSetItems(previousId).filter(id => !assignments.some(row => row.existing_product_id === id)) : [])
          return <div key={set.id} className="rounded-xl bg-slate-50 p-3">
            <h3 className="font-medium">{set.display_name} · 已入库单品关系</h3>
            <p className="text-sm text-slate-600">本批次单品可在右侧关系编辑区选择。</p>
            {oldItems.filter(row => !assignments.some(choice => choice.existing_product_id === row.id)).map(row =>
              <label key={row.id} className="mt-1 flex items-center gap-2 text-sm"><input type="checkbox" checked={proposed.includes(row.id)}
                onChange={event => choose({ batch_set_items: { ...choices.batch_set_items, [set.id]: toggle(proposed, row.id, event.target.checked) } })} />{row.product_code} {row.display_name}</label>)}
            <button type="button" className={`mt-2 ${linkClass}`} onClick={() => choose({ batch_set_items: { ...choices.batch_set_items, [set.id]: proposed } })}>
              {newSetOldItems(set.id) ? '已选，重新确认关系' : proposed.length ? '确认已入库单品关系' : '确认无已入库单品关系'}</button>
          </div>
        })}
        {required.map(id => {
          const row = oldProducts.find(row => row.id === id)
          if (!row) return null
          const proposed = confirmedRelations(id) ?? oldSetItems(id)
          const newIds = oldSetNewItems(id)
          return <div key={id} className="rounded-xl bg-amber-100/70 p-3">
            <h3 className="font-medium">复核关联 · {row.product_code} {row.display_name}</h3>
            {[...oldItems.map(item => ({ ...item, fresh: false })), ...newItems.map(item => ({ ...item, product_code: '待入库', fresh: true }))].map(item =>
              <label key={`${item.fresh ? 'batch' : 'catalog'}:${item.id}`} className="mt-1 flex items-center gap-2 text-sm">
                <input type="checkbox" checked={(item.fresh ? newIds : proposed).includes(item.id)} onChange={event => item.fresh
                  ? choose({ relation_batch_items: { ...choices.relation_batch_items, [id]: toggle(newIds, item.id, event.target.checked) } })
                  : updateOldSet(id, toggle(proposed, item.id, event.target.checked))} />{item.product_code} {item.display_name}</label>)}
            <button type="button" className={`mt-2 ${linkClass}`} onClick={() => updateOldSet(id, proposed)}>{confirmedRelations(id) ? '已复核，重新确认' : '确认关联关系'}</button>
          </div>
        })}
        {editable && <Button primary={false} disabled={busy || saving} onClick={() => void apply()}>保存身份与关联复核</Button>}
      </fieldset>
      {message && <p role="status" className="text-sm">{message}</p>}
    </div></details></Panel>
}
