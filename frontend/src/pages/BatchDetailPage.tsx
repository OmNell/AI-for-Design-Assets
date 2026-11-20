import { Button, Card, Panel } from '../components/ui'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getBatch, retryAi, type Batch, type BatchProduct } from '../api/upload'
import { getOperation, operationHref, operationLabels } from '../api/operations'
import { OperationDiff } from '../components/ops/OperationDiff'
import { OperationExecution } from '../components/ops/OperationExecution'
import { useAuthStore } from '../stores/auth'
import { IngestPanel } from '../components/IngestPanel'
import { TerminateBatchButton } from '../components/TerminateBatchButton'
import { ThumbnailPreview } from '../components/ThumbnailPreview'

const initialLabels: Record<string, string> = { category: '品类', material: '材质', color: '主色', style: '风格', festival: '节日场景', audience: '受众群体', usage_scene: '使用场景', size: '尺寸', craft: '工艺', material_name: '物料名称', structure: '结构' }

function Product({ product, batch }: { product: BatchProduct; batch: Batch }) {
  const file = batch.files.find(value => value.id === product.batch_file_id)
  const names = new Map(batch.products?.map(value => [value.id, value.display_name]))
  return <Card><div className="flex items-start justify-between gap-5"><div><h3 className="font-semibold">{product.display_name}</h3><p className="mt-2 text-xs text-slate-500">{product.kind === 'item' ? '单品' : '套装'} · {product.ai_status === 'done' ? '打标完成' : product.ai_status === 'failed' ? '打标失败' : product.ai_status === 'awaiting_description' ? '待填写描述或启动识别' : '等待处理'}</p></div>{file && <ThumbnailPreview batchId={batch.id} file={file} />}</div>
    {product.ai_status === 'done' && product.visual_ai_succeeded === false && <p className="mt-4 text-sm text-amber-800">当前主图缺少已验证的视觉识别结果，请重新打标。</p>}
    {product.ai_error && <p className="mt-4 text-sm text-red-800">{product.ai_error}</p>}
    {product.flags.includes('multi_object') && <p className="mt-4 rounded-xl bg-amber-100/65 p-3 text-sm text-amber-900">一图识别到多个商品</p>}
    {product.flags.includes('empty_relation') && <p className="mt-4 rounded-xl bg-amber-100/65 p-3 text-sm text-amber-900">套装推荐清单为空</p>}
    {!!Object.keys(product.tags).length && <dl className="mt-5 grid grid-cols-2 gap-3">{Object.entries(product.tags).map(([key, tag]) => <div key={key} className={`rounded-xl p-3 ${tag.flag === 'uncertain' ? 'bg-amber-100/65 text-amber-900' : 'bg-slate-50'}`}><dt className="mb-1 text-xs text-slate-500">{batch.tag_labels?.[key] ?? initialLabels[key] ?? key}{tag.flag === 'uncertain' ? ' · 无法判断' : ''}</dt><dd className="break-words text-sm">{tag.value || '—'}</dd></div>)}</dl>}
    {product.free_text && <p className="mt-5 whitespace-pre-wrap text-sm text-slate-600">{product.free_text}</p>}
    {product.kind === 'set' && product.ai_status === 'done' && <div className="mt-5 border-t border-slate-400/15 pt-4"><p className="mb-2 text-xs text-slate-500">推荐包含的单品（待人工核对）</p><p className="text-sm">{product.relation_items.map(item => names.get(item.batch_product_id)).filter(Boolean).join('、') || '暂无推荐'}</p></div>}
  </Card>
}

export function BatchDetailPage() {
  const id = Number(useParams().id)
  const [params] = useSearchParams()
  const replace = Number(params.get('replace')) || null
  const user = useAuthStore(state => state.user)
  const query = useQueryClient()
  const batch = useQuery({ queryKey: ['batch', id], queryFn: () => getBatch(id), enabled: Number.isInteger(id) && id > 0, retry: false,
    refetchInterval: result => ['AI 处理中', '入库中'].includes(result.state.data?.status ?? '') ? 1000 : false,
  })
  const isOperation = !!batch.data?.kind && batch.data.kind !== 'ingest'
  const operation = useQuery({ queryKey: ['operation', id], queryFn: () => getOperation(id), retry: false, enabled: isOperation &&
    ['待初审', '待终审', '入库中', '入库异常', '已入库', '已终止'].includes(batch.data?.status ?? '') })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const act = async (action: () => Promise<Batch>) => {
    setBusy(true); setError('')
    try { query.setQueryData(['batch', id], await action()); await query.invalidateQueries({ queryKey: ['batches'] }) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '操作未完成，请重试') }
    finally { setBusy(false) }
  }
  if (!Number.isInteger(id) || id <= 0) return <Panel><p role="alert">批次编号不正确</p></Panel>
  if (batch.isPending) return <Panel><p>正在加载批次…</p></Panel>
  if (batch.isError) return <Panel><p role="alert" className="text-red-800">{batch.error.message}</p></Panel>
  const value = batch.data
  const kind = value.kind && value.kind !== 'ingest' ? value.kind : null
  const light = kind !== null && kind !== 'supplement'
  const reviewing = ['待核对', '已驳回', '待初审', '待终审'].includes(value.status)
  const pending = ['待核对', '已驳回'].includes(value.status)
  const reviewPath = pending ? operationHref(id, value.kind, value.status) : `/batches/${id}/${light ? 'operation' : 'review'}`
  const suffix = replace && kind === 'supplement' ? `?replace=${replace}` : ''
  const progress = value.ai_progress ?? { items_total: 0, items_done: 0, sets_total: 0, sets_done: 0, failed: 0 }
  const canOperate = !!user && (value.owner_id === undefined || value.owner_id === user.id) && user.roles.some(role => ['uploader', 'first_reviewer', 'final_reviewer'].includes(role))
  const canRetry = canOperate || !!user?.roles.includes('admin')
  const products = value.products ?? []
  return <div className="space-y-6"><Panel><div className="mb-6 flex items-start justify-between gap-6"><div><p className="text-xs text-slate-500">{value.batch_no}</p><h1 className="mt-3 break-all text-2xl font-semibold">{value.folder_name}</h1></div><Link className="shrink-0 text-sm text-blue-700 underline underline-offset-4" to="/">返回首页</Link></div>
    <p className="text-sm text-emerald-800">{kind ? `${operationLabels[kind]} · ` : ''}{value.status}</p>
    {kind && value.project_id && <Link to={`/projects/${value.project_id}/operations`} className="mt-3 inline-block text-sm text-blue-700 underline">查看正式资产与操作</Link>}
    {!light && <p className="mt-4 break-all text-sm">推荐位置：{value.recommended_path || '位置待定'}</p>}
    {value.status === 'AI 处理中' && <p role="status" className="mt-4 text-sm text-slate-500">正在按单品、套装的顺序处理，请稍候…</p>}
    {error && <p role="alert" className="mt-4 text-red-800">{error}</p>}
    <div className="mt-6 flex flex-wrap gap-4">{value.status === '已暂存' && canOperate && (kind ? <Link to={`/batches/${id}/operation${suffix}`} className="self-center text-blue-700 underline">{kind === 'supplement' ? '核对补充文件' : '编辑操作草稿'}</Link>
      : <Link to={'/batches/' + id + '/pre-descriptions'} className="ui-button ui-primary">填写主图预描述</Link>)}
      {!light && canOperate && (pending || value.status === 'AI 处理失败') && <Link to={'/batches/' + id + '/pre-descriptions' + suffix}
        className="self-center text-blue-700 underline">查看或修改主图预描述</Link>}
      {!light && ['AI 处理中', 'AI 处理失败', '待核对'].includes(value.status) && (value.status === '待核对' ? user?.id === value.owner_id : canRetry) && <Button disabled={busy} onClick={() => void act(() => retryAi(id))}>重试整批 AI</Button>}
      {['上传中', '上传中断', '已暂存', 'AI 处理中', 'AI 处理失败', '待核对', '已驳回', '待初审', '待终审'].includes(value.status) && canOperate && (value.uploader_id === undefined || value.uploader_id === user?.id) && <TerminateBatchButton id={id} disabled={busy} onTerminated={result => {
        query.setQueryData(['batch', id], result)
        void query.invalidateQueries({ queryKey: ['batches'] })
      }} />}
      {reviewing && <Link to={`${reviewPath}${suffix}`} className="self-center text-blue-700 underline">{pending ? light ? '查看或编辑操作' : '进入核对页' : '查看提交版本'}</Link>}
      {['上传中', '上传中断'].includes(value.status) && canOperate && (value.uploader_id === undefined || value.uploader_id === user?.id) && <Link to={`/upload?resume=${id}${replace ? `&replace=${replace}` : ''}`} className="text-sm text-blue-700 underline underline-offset-4">继续上传</Link>}</div>
  </Panel>
    {['入库中', '入库异常', '已入库'].includes(value.status) && (kind
      ? <OperationExecution batchId={id} kind={kind} projectId={value.project_id} />
      : <IngestPanel batchId={id} />)}
    {operation.data && (operation.data.after || operation.data.purge) && <OperationDiff operation={operation.data} />}
    {!light && <div className="grid grid-cols-3 gap-5"><Card><p className="text-sm text-slate-500">单品完成</p><p className="mt-3 text-2xl">{progress.items_done} / {progress.items_total}</p></Card><Card><p className="text-sm text-slate-500">套装完成</p><p className="mt-3 text-2xl">{progress.sets_done} / {progress.sets_total}</p></Card><Card><p className="text-sm text-slate-500">失败</p><p className="mt-3 text-2xl">{progress.failed}</p></Card></div>}
    {value.termination_reason && <p className="text-sm text-slate-600">终止原因：{value.termination_reason}</p>}
    {!['已撤回', '已终止'].includes(value.status) && !!products.length && <div className="grid grid-cols-2 gap-5">{products.map(product => <Product key={product.id} product={product} batch={value} />)}</div>}
    {!light && value.status === '待核对' && <p className="text-sm text-slate-500">AI 处理已结束。本页只展示结果，请进入核对页逐项确认。</p>}
  </div>
}
