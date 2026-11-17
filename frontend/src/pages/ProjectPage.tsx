import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getProject, getProjectFiles, thumbUrl, type FileRow, type ProductBrief } from '../api/catalog'
import { downloadFiles, downloadMessage, triggerDownload, type BatchProgress } from '../lib/download'
import { checkFile } from '../api/catalog'
import { formatBytes } from '../lib/terms'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassSurface } from '../components/glass/GlassSurface'
import { GlassButton } from '../components/glass/GlassButton'
import { CopyLink } from '../components/CopyLink'
import { statusNames } from './ProjectsPage'
import { ProjectOps } from '../components/ops/ProjectOps'

const historyReasons: Record<string, string> = { replaced: '被替换', voided: '作废' }
const fileStatusNames: Record<string, string> = { replaced: '历史版本', voided: '已作废', deleted: '已删除', active: '已恢复' }

function ProductCard({ product, all }: { product: ProductBrief; all: Map<number, ProductBrief> }) {
  const [open, setOpen] = useState(false)
  const related = product.related.map(id => all.get(id)).filter((item): item is ProductBrief => !!item)
  return <div className="rounded-2xl border border-white/70 bg-white/35 p-3" data-product={product.product_code}>
    <Link to={`/products/${product.id}`} className="block"><img src={thumbUrl(product.id, 300)} alt={`${product.display_name} 缩略图`} loading="lazy" className="aspect-square w-full rounded-xl bg-white/40 object-contain" />
      <p className="mt-2 truncate text-sm font-medium" title={product.display_name}>{product.display_name}</p></Link>
    <p className="mt-1 flex items-center justify-between text-xs text-slate-500"><span>{product.product_code}</span><span>{product.kind === 'set' ? '套装' : '单品'} · {statusNames[product.status] ?? product.status}</span></p>
    {product.kind === 'set' && <button type="button" aria-expanded={open} className="mt-2 text-xs text-emerald-800 underline underline-offset-4" onClick={() => setOpen(!open)}>{open ? '收起' : `包含 ${related.length} 个单品`}</button>}
    {open && <ul className="mt-2 space-y-1 text-xs">{related.map(item => <li key={item.id}><Link to={`/products/${item.id}`} className="underline underline-offset-4">{item.product_code} {item.display_name}</Link></li>)}{!related.length && <li className="text-slate-500">不含单品</li>}</ul>}
  </div>
}

function Files({ projectId, category }: { projectId: number; category: string }) {
  const files = useQuery({ queryKey: ['project-files', projectId, category], queryFn: () => getProjectFiles(projectId, category), retry: false })
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [progress, setProgress] = useState<BatchProgress | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (files.isPending) return <p className="text-sm">正在加载文件…</p>
  if (files.isError) return <p role="alert" className="text-sm text-red-800">{files.error.message}</p>
  const rows = files.data
  const single = async (file: FileRow) => {
    setError(''); setProgress(null)
    try { await checkFile(file.id); triggerDownload(`/api/files/${file.id}/download`) } catch (reason: unknown) { setError(`${file.name}：${downloadMessage(reason)}`) }
  }
  const many = async () => {
    setBusy(true); setError('')
    try { await downloadFiles(rows.filter(row => checked.has(row.id)).map(row => row.id), setProgress) } catch (reason: unknown) { setError(downloadMessage(reason)) } finally { setBusy(false) }
  }
  const toggle = (id: number, on: boolean) => setChecked(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next })
  if (!rows.length) return <p className="text-sm text-slate-500">这个资料夹里没有文件。</p>
  return <div>
    <div className="mb-3 flex items-center gap-4 text-sm"><GlassButton disabled={busy || !checked.size} onClick={() => void many()}>下载所选（{checked.size}）</GlassButton>
      {progress && <span role="status" data-testid="download-progress">{progress.current ? `正在下载 ${Math.min(progress.done + 1, progress.total)} / ${progress.total}：${progress.current}` : `已依次触发 ${progress.total - progress.failures.length} 个下载${progress.failures.length ? `，${progress.failures.length} 个失败` : ''}`}</span>}</div>
    {error && <p role="alert" className="mb-3 text-sm text-red-800">{error}</p>}
    {!!progress?.failures.length && <ul role="alert" className="mb-3 space-y-1 text-sm text-red-800">{progress.failures.map(item => <li key={item.name}>{item.name}：{item.reason}</li>)}</ul>}
    <table className="w-full text-left text-sm"><thead className="text-xs text-slate-500"><tr><th className="w-10 p-2"><input type="checkbox" aria-label="全选" checked={checked.size === rows.length} onChange={event => setChecked(event.target.checked ? new Set(rows.map(row => row.id)) : new Set())} /></th>
      <th className="p-2">名称</th><th className="p-2">大小</th><th className="p-2">修改时间</th><th className="p-2" /></tr></thead>
      <tbody>{rows.map(file => <tr key={file.id} className="border-t border-slate-500/10">
        <td className="p-2"><input type="checkbox" aria-label={`选择 ${file.name}`} checked={checked.has(file.id)} onChange={event => toggle(file.id, event.target.checked)} /></td>
        <td className="p-2"><p className="break-all">{file.name}</p>{file.rel_path !== `${category}/${file.name}` && <p className="text-xs text-slate-500">{file.rel_path}</p>}</td>
        <td className="p-2 whitespace-nowrap">{formatBytes(file.size)}</td><td className="p-2 whitespace-nowrap">{new Date(file.mtime).toLocaleString('zh-CN')}</td>
        <td className="p-2 whitespace-nowrap"><button type="button" className="text-emerald-800 underline underline-offset-4" onClick={() => void single(file)}>下载</button><span className="ml-3"><CopyLink linkId={file.link_id} label="复制链接" /></span></td></tr>)}</tbody></table>
  </div>
}

export function ProjectPage() {
  const id = Number(useParams().id)
  const detail = useQuery({ queryKey: ['project', id], queryFn: () => getProject(id), enabled: Number.isInteger(id) && id > 0, retry: false })
  const [tab, setTab] = useState('')
  if (!Number.isInteger(id) || id <= 0) return <GlassPanel><p role="alert">项目编号不正确</p></GlassPanel>
  if (detail.isPending) return <GlassPanel><p>正在加载项目…</p></GlassPanel>
  if (detail.isError) return <GlassPanel><p role="alert" className="text-red-800">{detail.error.message}</p><Link to="/projects" className="mt-4 inline-block text-sm text-emerald-800 underline underline-offset-4">返回项目列表</Link></GlassPanel>
  const { project, folders } = detail.data
  // 物理删除的商品只保留记录（编号不再使用），不放进商品网格。
  const products = detail.data.products_list.filter(item => item.status !== 'deleted')
  const deleted = detail.data.products_list.length - products.length
  const all = new Map(products.map(item => [item.id, item]))
  const active = tab || folders[0]?.name || ''
  const position = project.current_path.split('/').slice(0, -1).join(' / ')
  return <div className="space-y-5">
    <GlassSurface radius={24} className="px-7 py-5">
      <div className="flex items-start justify-between gap-6"><div className="min-w-0"><h1 className="break-all text-xl font-semibold">{detail.data.folder_name}</h1>
        <p className="mt-2 text-sm text-slate-600">位置 {position} · 入库 {project.ingested_at ? new Date(project.ingested_at).toLocaleDateString('zh-CN') : '—'} · {project.won ? '已中标' : '未中标'} · {statusNames[project.status] ?? project.status}</p></div>
        <div className="flex shrink-0 items-center gap-3"><CopyLink linkId={detail.data.link_id} label="复制项目链接" /><Link to="/projects" className="text-sm text-emerald-800 underline underline-offset-4">返回列表</Link></div></div>
      <ProjectOps detail={detail.data} />
    </GlassSurface>
    <GlassPanel><h2 className="text-lg font-semibold">商品 {products.length} 个</h2>
      <div data-testid="products" className="mt-4 grid grid-cols-6 gap-4">{products.map(item => <ProductCard key={item.id} product={item} all={all} />)}</div>
      {!products.length && <p className="mt-3 text-sm text-slate-500">这个项目没有商品主图。</p>}
      {deleted > 0 && <p data-testid="deleted-products" className="mt-3 text-xs text-slate-500">另有 {deleted} 个商品已被物理删除，仅保留记录，编号不再使用。</p>}</GlassPanel>
    <GlassPanel><h2 className="text-lg font-semibold">资料夹</h2>
      <div role="tablist" className="mt-4 flex flex-wrap gap-2 text-sm">{folders.map(folder => <button key={folder.name} type="button" role="tab" aria-selected={active === folder.name}
        className={`rounded-full px-4 py-1.5 ${active === folder.name ? 'bg-emerald-800 text-white' : 'bg-white/50 text-slate-700 hover:bg-white/80'}`} onClick={() => setTab(folder.name)}>{folder.name === 'other' ? '其他' : folder.name}（{folder.count}）</button>)}</div>
      <div className="mt-5">{active && <Files key={active} projectId={id} category={active} />}</div></GlassPanel>
    <GlassPanel><h2 className="text-lg font-semibold">历史版本</h2>{detail.data.history.length ? <table data-testid="history" className="mt-3 w-full text-left text-sm"><tbody>{detail.data.history.map(item =>
      <tr key={item.id} className="border-t border-slate-500/10"><td className="w-28 p-2 text-slate-500">{new Date(item.created_at).toLocaleDateString('zh-CN')}</td>
        <td className="p-2"><p className="break-all">{item.product_code ? `${item.product_code} · ` : ''}{item.old_rel_path} → {item.new_rel_path}</p></td>
        <td className="w-20 p-2">{historyReasons[item.reason] ?? item.reason}</td><td className="w-24 p-2 text-slate-500">{fileStatusNames[item.file_status] ?? item.file_status}</td>
        <td className="w-24 p-2 whitespace-nowrap">{item.file_status !== 'deleted' && <button type="button" className="text-emerald-800 underline underline-offset-4" onClick={() => triggerDownload(`/api/files/${item.file_id}/download`)}>下载</button>}</td></tr>)}</tbody></table> : <p className="mt-3 text-sm text-slate-500">（无）</p>}</GlassPanel>
    <GlassPanel><h2 className="text-lg font-semibold">操作记录</h2>
      <table data-testid="logs" className="mt-3 w-full text-left text-sm"><tbody>{detail.data.logs.map((item, index) => <tr key={index} className="border-t border-slate-500/10"><td className="w-48 p-2 text-slate-500">{new Date(item.ts).toLocaleString('zh-CN')}</td><td className="w-40 p-2">{item.user}</td><td className="p-2">{item.action}</td><td className="w-20 p-2">{item.result}</td></tr>)}</tbody></table></GlassPanel>
  </div>
}
