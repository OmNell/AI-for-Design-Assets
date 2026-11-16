import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getBatch, confirmBatch, cancelBatch, retryAi, type Batch, type BatchProduct } from '../api/upload'
import { useAuthStore } from '../stores/auth'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassCard } from '../components/glass/GlassCard'
import { GlassButton } from '../components/glass/GlassButton'
import { ThumbnailPreview } from '../components/ThumbnailPreview'

const initialLabels: Record<string, string> = { category: '品类', material: '材质', color: '主色', style: '风格', festival: '节日场景', audience: '受众群体', usage_scene: '使用场景', size: '尺寸', craft: '工艺', material_name: '物料名称', structure: '结构' }

function Product({ product, batch }: { product: BatchProduct; batch: Batch }) {
  const file = batch.files.find(value => value.id === product.batch_file_id)
  const names = new Map(batch.products?.map(value => [value.id, value.display_name]))
  return <GlassCard><div className="flex items-start justify-between gap-5"><div><h3 className="font-semibold">{product.display_name}</h3><p className="mt-2 text-xs text-slate-500">{product.kind === 'item' ? '单品' : '套装'} · {product.ai_status === 'done' ? '打标完成' : product.ai_status === 'failed' ? '打标失败' : '等待处理'}</p></div>{file && <ThumbnailPreview batchId={batch.id} file={file} />}</div>
    {product.ai_error && <p className="mt-4 text-sm text-red-800">{product.ai_error}</p>}
    {product.flags.includes('multi_object') && <p className="mt-4 rounded-xl bg-amber-100/65 p-3 text-sm text-amber-900">一图识别到多个商品</p>}
    {product.flags.includes('empty_relation') && <p className="mt-4 rounded-xl bg-amber-100/65 p-3 text-sm text-amber-900">套装推荐清单为空</p>}
    {!!Object.keys(product.tags).length && <dl className="mt-5 grid grid-cols-2 gap-3">{Object.entries(product.tags).map(([key, tag]) => <div key={key} className={`rounded-xl p-3 ${tag.flag === 'uncertain' ? 'bg-amber-100/65 text-amber-900' : 'bg-white/20'}`}><dt className="mb-1 text-xs text-slate-500">{batch.tag_labels?.[key] ?? initialLabels[key] ?? key}{tag.flag === 'uncertain' ? ' · 无法判断' : ''}</dt><dd className="break-words text-sm">{tag.value || '—'}</dd></div>)}</dl>}
    {product.free_text && <p className="mt-5 whitespace-pre-wrap text-sm text-slate-600">{product.free_text}</p>}
    {product.kind === 'set' && product.ai_status === 'done' && <div className="mt-5 border-t border-slate-400/15 pt-4"><p className="mb-2 text-xs text-slate-500">推荐包含的单品（待人工核对）</p><p className="text-sm">{product.relation_items.map(item => names.get(item.batch_product_id)).filter(Boolean).join('、') || '暂无推荐'}</p></div>}
  </GlassCard>
}

export function BatchDetailPage() {
  const id = Number(useParams().id)
  const user = useAuthStore(state => state.user)
  const query = useQueryClient()
  const batch = useQuery({ queryKey: ['batch', id], queryFn: () => getBatch(id), enabled: Number.isInteger(id) && id > 0, retry: false,
    refetchInterval: result => result.state.data?.status === 'AI 处理中' ? 1000 : false,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const act = async (action: () => Promise<Batch>) => {
    setBusy(true); setError('')
    try { query.setQueryData(['batch', id], await action()); await query.invalidateQueries({ queryKey: ['batches'] }) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '操作未完成，请重试') }
    finally { setBusy(false) }
  }
  if (!Number.isInteger(id) || id <= 0) return <GlassPanel><p role="alert">批次编号不正确</p></GlassPanel>
  if (batch.isPending) return <GlassPanel><p>正在加载批次…</p></GlassPanel>
  if (batch.isError) return <GlassPanel><p role="alert" className="text-red-800">{batch.error.message}</p></GlassPanel>
  const value = batch.data
  const progress = value.ai_progress ?? { items_total: 0, items_done: 0, sets_total: 0, sets_done: 0, failed: 0 }
  const canOperate = !!user && (value.uploader_id === undefined || value.uploader_id === user.id) && user.roles.some(role => ['uploader', 'first_reviewer', 'final_reviewer'].includes(role))
  const canRetry = canOperate || !!user?.roles.includes('admin')
  const products = value.products ?? []
  return <div className="space-y-6"><GlassPanel><div className="mb-6 flex items-start justify-between gap-6"><div><p className="text-xs text-slate-500">{value.batch_no}</p><h1 className="mt-3 break-all text-2xl font-semibold">{value.folder_name}</h1></div><Link className="shrink-0 text-sm text-emerald-800 underline underline-offset-4" to="/">返回首页</Link></div>
    <p className="text-sm text-emerald-800">{value.status}</p><p className="mt-4 break-all text-sm">推荐位置：{value.recommended_path || '位置待定'}</p>
    {value.status === 'AI 处理中' && <p role="status" className="mt-4 text-sm text-slate-500">正在按单品、套装的顺序处理，请稍候…</p>}
    {error && <p role="alert" className="mt-4 text-red-800">{error}</p>}
    <div className="mt-6 flex gap-4">{value.status === '已暂存' && canOperate && <GlassButton disabled={busy} onClick={() => void act(() => confirmBatch(id))}>确认，开始 AI 处理</GlassButton>}
      {['AI 处理中', 'AI 处理失败', '待核对'].includes(value.status) && canRetry && <GlassButton disabled={busy} onClick={() => void act(() => retryAi(id))}>重试整批 AI</GlassButton>}
      {value.status === 'AI 处理中' && canOperate && <GlassButton disabled={busy} primary={false} onClick={() => void act(() => cancelBatch(id))}>撤回批次</GlassButton>}
      {['待核对', '已驳回'].includes(value.status) && <Link to={`/batches/${id}/review`} className="self-center text-sm font-medium text-emerald-800 underline underline-offset-4">进入核对页</Link>}
      {['上传中', '上传中断'].includes(value.status) && canOperate && <Link to={`/upload?resume=${id}`} className="text-sm text-emerald-800 underline underline-offset-4">继续上传</Link>}</div>
  </GlassPanel>
    <div className="grid grid-cols-3 gap-5"><GlassCard><p className="text-sm text-slate-500">单品完成</p><p className="mt-3 text-2xl">{progress.items_done} / {progress.items_total}</p></GlassCard><GlassCard><p className="text-sm text-slate-500">套装完成</p><p className="mt-3 text-2xl">{progress.sets_done} / {progress.sets_total}</p></GlassCard><GlassCard><p className="text-sm text-slate-500">失败</p><p className="mt-3 text-2xl">{progress.failed}</p></GlassCard></div>
    {value.status !== '已撤回' && !!products.length && <div className="grid grid-cols-2 gap-5">{products.map(product => <Product key={product.id} product={product} batch={value} />)}</div>}
    {['待核对', '已驳回'].includes(value.status) && <p className="text-sm text-slate-500">AI 处理已结束。本页只展示结果，修改标签、确认套装关系和提交审核请到核对页。</p>}
  </div>
}
