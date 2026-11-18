import type { ReviewField, ReviewProduct } from '../../api/review'
import { GlassListItem } from '../glass/GlassListItem'
import { Thumb } from './Thumb'

export function marks(product: ReviewProduct, fields: ReviewField[]) {
  const yellow = product.flags.includes('multi_object') || fields.some(field => product.tags[field.key]?.flag) || (product.kind === 'set' && !product.relation_confirmed)
  return { yellow, red: product.ai_status === 'failed' || (product.ai_status === 'done' && !product.visual_ai_succeeded), busy: product.ai_status === 'pending' }
}

interface Props {
  batchId: number; products: ReviewProduct[]; fields: ReviewField[]; selectedId: number | undefined; onSelect: (id: number) => void
  /** 提供时每行显示多选框（核对页）；审核页不提供。 */
  checked?: Set<number>; onToggle?: (id: number, on: boolean) => void
  /** 每张图上钉了几条意见。 */
  pinCounts?: Record<number, number>
}

/** 核对页与审核页共用的分组主图列表。 */
export function ProductList({ batchId, products, fields, selectedId, onSelect, checked, onToggle, pinCounts = {} }: Props) {
  const group = (kind: 'set' | 'item', title: string) => {
    const rows = products.filter(product => product.kind === kind)
    return <details open className="mb-3"><summary className="mb-2 cursor-pointer select-none text-xs font-medium text-slate-500">{title}（{rows.length}）</summary>
      <ul className="space-y-1.5">{rows.map(product => {
        const mark = marks(product, fields)
        return <li key={product.id} className="review-row"><GlassListItem active={product.id === selectedId}>
          <div className="flex items-center gap-2 px-2 py-1.5">
            {checked && onToggle && <input type="checkbox" aria-label={`选择 ${product.display_name}`} checked={checked.has(product.id)} onChange={event => onToggle(product.id, event.target.checked)} />}
            <button type="button" aria-current={product.id === selectedId} className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={() => onSelect(product.id)}>
              <Thumb batchId={batchId} fileId={product.batch_file_id} version={product.fingerprint ?? ''} alt={`${product.display_name} 缩略图`} />
              <span className="min-w-0 flex-1 truncate text-sm" title={product.display_name}>{product.display_name}</span>
              {!!pinCounts[product.id] && <span className="shrink-0 rounded-full bg-red-600 px-1.5 text-[11px] text-white" title="图上钉有审核意见">钉 {pinCounts[product.id]}</span>}
              {mark.busy && <span className="shrink-0 rounded-full bg-sky-600 px-1.5 text-[11px] text-white" title="正在打标">…</span>}
              {mark.yellow && <span className="shrink-0 rounded-full bg-amber-500 px-1.5 text-[11px] text-white" title="有待处理的黄色项">黄</span>}
              {mark.red && <span className="shrink-0 rounded-full bg-red-600 px-1.5 text-[11px] text-white" title="打标失败，需重新识别">红</span>}
            </button></div></GlassListItem></li>
      })}</ul>
      {!rows.length && <p className="text-xs text-slate-400">没有{title}主图</p>}</details>
  }
  return <>{group('set', '套装')}{group('item', '单品')}</>
}

const stageNames: Record<string, string> = { first: '初审', final: '终审' }
/** 历次审核意见（最新在前）。 */
export function RoundHistory({ history }: { history: { round: number; stage: string; reviewer_name: string; decision: string; comment: string | null; decided_at: string | null; pins: { product_name: string; text: string }[] }[] }) {
  if (!history.length) return <p className="text-xs text-slate-500">还没有审核记录。</p>
  return <ol className="space-y-2">{history.map((item, index) => <li key={index} className={`rounded-xl p-3 text-xs ${item.decision === 'reject' ? 'bg-red-100/70 text-red-900' : 'bg-white/30 text-slate-700'}`}>
    <p className="font-medium">第 {item.round} 轮 · {stageNames[item.stage] ?? item.stage}{item.decision === 'reject' ? '驳回' : '通过'} · {item.reviewer_name}{item.decided_at ? ` · ${new Date(item.decided_at).toLocaleString('zh-CN')}` : ''}</p>
    {item.comment && <p className="mt-1 whitespace-pre-wrap">{item.comment}</p>}
    {!!item.pins.length && <ul className="mt-1 list-disc pl-4">{item.pins.map((pin, key) => <li key={key}>{pin.product_name}：{pin.text || '（未写文字）'}</li>)}</ul>}</li>)}</ol>
}
