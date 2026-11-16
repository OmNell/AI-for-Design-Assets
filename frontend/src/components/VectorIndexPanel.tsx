import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getIndexStatus, rebuildIndex } from '../api/search'
import { GlassPanel } from './glass/GlassPanel'
import { GlassButton } from './glass/GlassButton'

/** 向量索引状态与「重建全部向量」：换了向量模型或维度之后使用。 */
export function VectorIndexPanel() {
  const client = useQueryClient()
  const status = useQuery({ queryKey: ['search-index'], queryFn: getIndexStatus, retry: false, refetchInterval: result => result.state.data?.queued ? 2000 : false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const rebuild = async () => {
    setBusy(true); setError('')
    try { client.setQueryData(['search-index'], await rebuildIndex()) } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '重建没有开始，请重试') } finally { setBusy(false) }
  }
  const data = status.data
  return <GlassPanel><h2 className="text-xl font-semibold">向量索引</h2>
    <p className="mt-3 text-sm text-slate-500">搜索时用来找「意思相近」的商品。更换向量模型或维度后，旧向量不再可用，需要重建。</p>
    {status.isPending ? <p className="mt-4 text-sm text-slate-500">正在读取索引状态…</p> : status.isError ? <p role="alert" className="mt-4 text-sm text-red-800">{status.error.message}</p> : data && typeof data.vectors === 'number' &&
      <p data-testid="index-status" className="mt-4 text-sm">已有向量 {data.vectors} / 有效商品 {data.products} · 实现 {data.backend}{data.model ? ` · 模型 ${data.model}（${data.dim} 维）` : ''}{data.queued ? ` · 还有 ${data.queued} 个在排队` : ''}</p>}
    {error && <p role="alert" className="mt-3 text-sm text-red-800">{error}</p>}
    <div className="mt-5"><GlassButton primary={false} disabled={busy || !!data?.queued} onClick={() => void rebuild()}>重建全部向量</GlassButton></div>
  </GlassPanel>
}
