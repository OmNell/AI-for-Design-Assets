import { useState } from 'react'
import { Link, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getProject, getProjectFiles, thumbUrl, type ProductBrief } from '../api/catalog'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassCard } from '../components/glass/GlassCard'
import { GlassButton } from '../components/glass/GlassButton'
import { CopyLink } from '../components/CopyLink'
import { formatBytes } from '../lib/terms'
import { describeError, downloadFile, sequentialDownload, type BatchTransfer } from '../lib/download'

function ProductCard({ item, related }: { item: ProductBrief; related: ProductBrief[] }) {
  const [broken, setBroken] = useState(false)
  return <GlassCard>
    <Link to={`/products/${item.id}`}>
      <div className="grid aspect-[4/3] place-items-center overflow-hidden rounded-xl bg-white/40">
        {broken ? <p className="text-xs text-slate-500">预览不可用，请打开商品详情</p> :
          <img src={thumbUrl(item.id)} loading="lazy" alt={item.display_name} onError={() => setBroken(true)}
            className="h-full w-full object-contain" />}
      </div>
      <h3 className="mt-3 break-words font-semibold text-emerald-900">{item.display_name}</h3>
    </Link>
    <p className="mt-1 text-xs text-slate-600">{item.product_code} · {item.kind === 'set' ? '套装' : '单品'}</p>
    <ul aria-label="主要标签" className="mt-2 flex flex-wrap gap-1">{item.tags.map((tag, index) =>
      <li key={index} className="rounded-full bg-white/65 px-2 py-0.5 text-xs text-slate-700">{tag}</li>)}</ul>
    {item.kind === 'set' && <details className="mt-3 text-sm"><summary className="cursor-pointer">包含 {related.length} 个单品</summary>
      <ul className="mt-2 space-y-1">{related.map(row => <li key={row.id}><Link to={`/products/${row.id}`} className="underline">{row.display_name}</Link></li>)}</ul></details>}
    {item.link_url && <a href={item.link_url} className="mt-2 inline-block text-xs text-emerald-800 underline">商品永久链接</a>}
  </GlassCard>
}

function FilesSection({ projectId, folders }: { projectId: number; folders: { name: string; count: number }[] }) {
  const [category, setCategory] = useState(folders[0]?.name ?? '01定稿文件')
  const [selected, setSelected] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const [transfer, setTransfer] = useState<BatchTransfer>()
  const [error, setError] = useState('')
  const query = useQuery({ queryKey: ['project-files', projectId, category], queryFn: () => getProjectFiles(projectId, category) })
  const doDownload = async (id?: number) => {
    setBusy(true)
    setError('')
    setTransfer(undefined)
    try {
      if (id !== undefined) await downloadFile(id)
      else await sequentialDownload(selected, setTransfer)
    } catch (reason: unknown) {
      setError(describeError(reason))
    } finally { setBusy(false) }
  }
  const rows = query.data ?? []
  return <GlassPanel>
    <div className="flex items-center justify-between"><h2 className="text-xl font-semibold">项目资料</h2>
      <GlassButton disabled={busy || !selected.length} onClick={() => void doDownload()}>下载所选文件{selected.length ? `（${selected.length}）` : ''}</GlassButton>
    </div>
    <div className="my-5 flex flex-wrap gap-2" role="tablist" aria-label="资料夹">
      {folders.map(folder => <button key={folder.name} type="button" role="tab" aria-selected={category === folder.name} disabled={busy}
        className={`rounded-full border border-white/60 px-4 py-2 text-sm ${category === folder.name ? 'bg-emerald-800 text-white' : 'bg-white/50 text-slate-700'}`}
        onClick={() => { setCategory(folder.name); setSelected([]); setTransfer(undefined) }}>{folder.name} <span className="opacity-75">{folder.count}</span></button>)}
    </div>
    {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    {transfer && <div aria-live="polite" className="mb-4 rounded-xl bg-white/50 p-3 text-sm">
      <p>共 {transfer.total} 个 · 已发送 {transfer.sent} 个 · 失败 {transfer.failed.length} 个</p>
      {busy && transfer.current && <p className="mt-1 text-slate-600">正在处理 {transfer.current}</p>}
      {transfer.failed.map((row, index) => <p key={index} className="text-red-800">{row.name} · {row.message}</p>)}
    </div>}
    {query.isPending ? <p>正在读取文件…</p> : query.isError ? <p role="alert" className="text-red-800">{query.error.message}</p> :
      !rows.length ? <p className="text-slate-500">此资料夹暂无文件。</p> :
      <table className="w-full text-left text-sm">
        <thead><tr className="text-slate-500"><th className="p-2"><input type="checkbox" aria-label="选择全部文件" checked={selected.length === rows.length}
          onChange={event => setSelected(event.target.checked ? rows.map(row => row.id) : [])} /></th>
          <th className="p-2">文件</th><th className="p-2">类型</th><th className="p-2">大小</th><th className="p-2">修改时间</th><th className="p-2">操作</th></tr></thead>
        <tbody>{rows.map(row => <tr key={row.id} className="border-t border-slate-400/20">
          <td className="p-2"><input type="checkbox" aria-label={`选择 ${row.name}`} checked={selected.includes(row.id)}
            onChange={event => setSelected(current => event.target.checked ? [...current, row.id] : current.filter(id => id !== row.id))} /></td>
          <td className="p-2 break-all"><Link className="text-emerald-800 underline" to={`/files/${row.id}`}>{row.name}</Link>
            {row.rel_path !== `${category}/${row.name}` && <p className="mt-1 text-xs text-slate-500">{row.rel_path}</p>}</td>
          <td className="p-2"><span className="rounded bg-slate-200/75 px-2 py-1 text-xs">{row.is_image ? '图片' : (row.name.includes('.') ? row.name.split('.').at(-1)?.toUpperCase() : '文件')}</span></td>
          <td className="p-2 whitespace-nowrap">{formatBytes(row.size)}</td>
          <td className="p-2 whitespace-nowrap">{new Date(row.mtime).toLocaleString('zh-CN')}</td>
          <td className="p-2"><button type="button" disabled={busy} className="text-emerald-800 underline" onClick={() => void doDownload(row.id)}>下载</button>
            {row.link_url && <a href={row.link_url} className="ml-3 whitespace-nowrap text-xs text-emerald-800 underline">永久链接</a>}</td>
        </tr>)}</tbody>
      </table>}
  </GlassPanel>
}

export function ProjectPage() {
  const id = Number(useParams().id)
  const valid = Number.isSafeInteger(id) && id > 0
  const detail = useQuery({ queryKey: ['project', id], queryFn: () => getProject(id), enabled: valid })
  if (!valid) return <GlassPanel><p role="alert">项目编号不正确</p></GlassPanel>
  if (detail.isPending) return <GlassPanel><p role="status">正在读取项目…</p></GlassPanel>
  if (detail.isError) return <GlassPanel><p role="alert">{detail.error.message}</p><Link to="/projects" className="underline">返回项目列表</Link></GlassPanel>
  const { project, products_list, folders, history, logs } = detail.data
  const products = new Map(products_list.map(item => [item.id, item]))
  return <div className="space-y-5">
    <GlassPanel><div className="flex items-start justify-between gap-6"><div>
      <Link to="/projects" className="text-sm text-emerald-800 underline">返回项目列表</Link>
      <p className="mt-4 text-sm text-slate-600">{project.project_code} · {project.won ? '已中标' : '未中标'}</p>
      <h1 className="mt-2 break-all text-3xl font-semibold">{project.project_name}</h1>
      <p className="mt-3 text-sm text-slate-600">{project.company_name}{project.industry_name && ` · ${project.industry_name}`}
        {project.ingested_at && ` · 入库于 ${new Date(project.ingested_at).toLocaleDateString('zh-CN')}`}</p>
    </div><div className="flex shrink-0 items-center gap-3">
      <Link to={`/projects/${id}/operations`} className="text-sm text-emerald-800 underline">资产操作与作废记录</Link>
      <CopyLink url={project.link_url} label="复制项目链接" /></div></div></GlassPanel>
    <section><h2 className="mb-4 text-xl font-semibold">商品 · {project.products}</h2>
      {products_list.length ? <div className="grid grid-cols-3 gap-4">{products_list.map(item =>
        <ProductCard key={item.id} item={item} related={item.related.flatMap(id => { const row = products.get(id); return row ? [row] : [] })} />)}</div> :
        <GlassPanel><p className="text-slate-500">暂无有效商品。</p></GlassPanel>}</section>
    <FilesSection key={id} projectId={id} folders={folders} />
    {!!history.length && <GlassPanel><h2 className="text-lg font-semibold">历史版本</h2><ul className="mt-3 space-y-2 text-sm">{history.map(row =>
      <li key={row.id}><Link to={`/files/${row.file_id}`} className="underline">{row.old_rel_path}</Link> · {new Date(row.created_at).toLocaleDateString('zh-CN')} <Link to={`/projects/${id}/operations?file=${row.file_id}`} className="ml-3 text-emerald-800 underline">版本操作</Link></li>)}</ul></GlassPanel>}
    {!!logs.length && <GlassPanel><details><summary className="cursor-pointer font-semibold">操作记录</summary>
      <ul className="mt-3 space-y-2 text-sm">{logs.map((row, index) => <li key={index}>{new Date(row.ts).toLocaleString('zh-CN')} · {row.user} · {row.action} · {row.result}</li>)}</ul></details></GlassPanel>}
  </div>
}
