import { Link, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getProduct, thumbUrl } from '../api/catalog'
import { downloadMessage, triggerDownload } from '../lib/download'
import { checkFile } from '../api/catalog'
import { useState } from 'react'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassSurface } from '../components/glass/GlassSurface'
import { GlassButton } from '../components/glass/GlassButton'
import { CopyLink } from '../components/CopyLink'
import { statusNames } from './ProjectsPage'

const sources: Record<string, string> = { ai: 'AI', manual: '人工', ai_confirmed: 'AI 已确认' }

export function ProductPage() {
  const id = Number(useParams().id)
  const product = useQuery({ queryKey: ['product', id], queryFn: () => getProduct(id), enabled: Number.isInteger(id) && id > 0, retry: false })
  const [error, setError] = useState('')
  if (!Number.isInteger(id) || id <= 0) return <GlassPanel><p role="alert">商品编号不正确</p></GlassPanel>
  if (product.isPending) return <GlassPanel><p>正在加载商品…</p></GlassPanel>
  if (product.isError) return <GlassPanel><p role="alert" className="text-red-800">{product.error.message}</p></GlassPanel>
  const data = product.data
  const download = async () => { setError(''); try { await checkFile(data.file_id); triggerDownload(`/api/files/${data.file_id}/download`) } catch (reason: unknown) { setError(downloadMessage(reason)) } }
  return <div className="grid grid-cols-[minmax(0,1fr)_440px] gap-5">
    <GlassSurface radius={24} className="p-4"><img src={thumbUrl(data.id, 1600)} alt={`${data.display_name} 主图`} className="max-h-[78vh] w-full rounded-2xl bg-white/30 object-contain" /></GlassSurface>
    <GlassPanel>
      <p className="text-xs text-slate-500">{data.product_code} · {data.kind === 'set' ? '套装' : '单品'} · {statusNames[data.status] ?? data.status}</p>
      <h1 className="mt-2 break-all text-2xl font-semibold">{data.display_name}</h1>
      <p className="mt-3 text-sm">所属项目：<Link to={`/projects/${data.project.id}`} className="text-emerald-800 underline underline-offset-4">{data.project.project_code} {data.project.company_name} {data.project.project_name}</Link></p>
      <div className="mt-4 flex flex-wrap items-center gap-2"><CopyLink linkId={data.product_link} label="复制商品链接" /><CopyLink linkId={data.project_link} label="复制项目链接" />
        <GlassButton primary={false} onClick={() => void download()}>下载主图原件</GlassButton></div>
      {error && <p role="alert" className="mt-3 text-sm text-red-800">{error}</p>}
      <h2 className="mt-6 text-sm font-semibold">标签</h2>
      <dl data-testid="tags" className="mt-2 space-y-2">{data.tags.map(tag => <div key={tag.key} className="grid grid-cols-[84px_minmax(0,1fr)_auto] gap-2 text-sm"><dt className="text-slate-500">{tag.label}</dt>
        <dd className="flex flex-wrap gap-1.5">{tag.terms.length ? tag.terms.map(term => <span key={term} className="rounded-full bg-white/60 px-2.5 py-0.5 text-xs">{term}</span>) : <span className="text-slate-400">—</span>}</dd>
        <dd className="text-[11px] text-slate-400">{sources[tag.source] ?? tag.source}</dd></div>)}</dl>
      {data.free_text && <><h2 className="mt-6 text-sm font-semibold">自由描述</h2><p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{data.free_text}</p></>}
      <h2 className="mt-6 text-sm font-semibold">{data.kind === 'set' ? '包含的单品' : '所属套装'}</h2>
      <ul data-testid="related" className="mt-2 space-y-1.5 text-sm">{data.related.map(item => <li key={item.id}><Link to={`/products/${item.id}`} className="text-emerald-800 underline underline-offset-4">{item.product_code} {item.display_name}</Link></li>)}
        {!data.related.length && <li className="text-slate-500">{data.kind === 'set' ? '不含单品' : '不属于任何套装'}</li>}</ul>
    </GlassPanel>
  </div>
}
