import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { buildIndex, changeIndex, getIndexes, type IndexAction, type IndexStatus } from '../api/search'
import { GlassPanel } from './glass/GlassPanel'
import { GlassButton } from './glass/GlassButton'

const labels = { formal: '正式', candidate: '候选', rollback: '可回滚' }
export function VectorIndexPanel() {
  const cache = useQueryClient()
  const key = ['vector-indexes']
  const status = useQuery({ queryKey: key, queryFn: getIndexes, refetchInterval: 3000 })
  const [name, setName] = useState('')
  const [version, setVersion] = useState('')
  const mutation = useMutation({
    mutationFn: (run: () => Promise<IndexStatus>) => run(),
    onSuccess: data => { cache.setQueryData(key, data) },
  })
  const act = (generation: number, action: IndexAction) => {
    if (action === 'switch' && !window.confirm('切换正式索引？新查询与入库将使用这个模型。')) return
    if (action === 'rollback' && !window.confirm('回滚到上一正式索引？')) return
    mutation.mutate(() => changeIndex(generation, action))
  }

  return <GlassPanel>
    <h2 className="text-xl font-semibold">向量索引</h2>
    {status.isPending && <p className="mt-4 text-sm">正在读取索引…</p>}
    {status.isError && <p role="alert" className="mt-4 text-red-800">{status.error.message}</p>}
    {status.data && <>
      <p className="mt-2 text-sm text-slate-600">相似度计算 · {status.data.backend}</p>
      {!status.data.indexes.length ? <p className="mt-4 text-sm">首个项目正式入库后可建立候选索引。</p> :
        <ul className="my-4 space-y-3">{status.data.indexes.map(row => <li key={row.generation} className="rounded-xl bg-white/45 p-4 text-sm">
          <p className="font-semibold">{labels[row.status]} · 第 {row.generation} 代 · {row.name}</p>
          <p className="mt-2 text-xs text-slate-600">版本 {row.revision || row.version.slice(0, 12)} · 维度 {row.dimension ?? '待检测'}</p>
          <p className="mt-2">已校验 {row.ready} / {row.total} · 待生成 {row.pending} · 失败 {row.failures}
            {row.running ? ' · 构建中' : row.verified ? ' · 完整性验证通过' : ' · 待完成校验'}</p>
          {row.status !== 'formal' && <div className="mt-3 flex gap-2">
            <GlassButton primary={false} disabled={mutation.isPending || row.running} onClick={() => act(row.generation, 'retry')}>补齐索引</GlassButton>
            <GlassButton disabled={mutation.isPending || row.running || !row.verified}
              onClick={() => act(row.generation, row.status === 'candidate' ? 'switch' : 'rollback')}>{row.status === 'candidate' ? '切换为正式' : '回滚'}</GlassButton>
            {row.status === 'candidate' && <GlassButton primary={false} disabled={mutation.isPending}
              onClick={() => act(row.generation, 'cancel')}>取消候选</GlassButton>}
          </div>}
        </li>)}</ul>}
      {status.data.indexes.some(row => row.status === 'formal') && !status.data.indexes.some(row => row.status === 'candidate') &&
        <form className="mt-4 flex gap-3" onSubmit={event => { event.preventDefault(); mutation.mutate(() => buildIndex(name.trim(), version.trim())) }}>
          <input aria-label="候选模型名称" required maxLength={100} className="field !py-2" placeholder="候选模型名称" value={name} onChange={event => setName(event.target.value)} />
          <input aria-label="候选模型版本" maxLength={80} className="field !py-2" placeholder="版本（可选）" value={version} onChange={event => setVersion(event.target.value)} />
          <GlassButton type="submit" className="shrink-0" disabled={mutation.isPending || !name.trim()}>构建候选索引</GlassButton>
        </form>}
    </>}
    {mutation.error && <p role="alert" className="mt-4 text-red-800">{mutation.error.message}</p>}
  </GlassPanel>
}
