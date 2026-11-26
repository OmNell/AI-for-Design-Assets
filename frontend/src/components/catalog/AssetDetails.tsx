import { useState } from 'react'
import { Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { checkFile, getProduct, thumbUrl, type FileRow, type ProjectRow } from '../../api/catalog'
import { useAuthStore } from '../../stores/auth'
import { describeError, triggerDownload } from '../../lib/download'
import { formatBytes } from '../../lib/terms'
import { CopyLink } from '../CopyLink'
import { AssetPreview } from '../AssetPreview'
import { Badge, Button, Dialog, ErrorState, FileThumbnail, Icon, LoadingState } from '../ui'

export function useAssetOperations() {
  return useAuthStore(state => state.user?.roles.some(role => ['uploader', 'first_reviewer', 'final_reviewer'].includes(role)) ?? false)
}

export function ProjectDetails({ row }: { row: ProjectRow }) {
  const operations = useAssetOperations()
  return <div className="space-y-4"><h3 className="break-words text-base font-semibold">{row.project_name}</h3>
    <div className="flex flex-wrap gap-2"><Badge tone={row.won ? 'success' : 'neutral'}>{row.won ? '已中标' : '未中标'}</Badge><Badge>{row.project_code}</Badge></div>
    <dl className="space-y-3 border-t border-slate-200 pt-4 text-sm">
      <div><dt className="text-xs text-slate-500">企业</dt><dd className="mt-1 break-words">{row.company_name}</dd></div>
      <div><dt className="text-xs text-slate-500">行业</dt><dd className="mt-1">{row.industry_name || '—'}</dd></div>
      <div><dt className="text-xs text-slate-500">资产</dt><dd className="mt-1">{row.products} 件商品 · {row.files} 个文件</dd></div>
      <div><dt className="text-xs text-slate-500">入库时间</dt><dd className="mt-1">{row.ingested_at ? new Date(row.ingested_at).toLocaleDateString('zh-CN') : '—'}</dd></div>
    </dl>
    <div className="flex flex-wrap items-center gap-2"><Link className="ui-button ui-primary" to={`/projects/${row.id}`}>打开项目</Link><CopyLink url={row.link_url} /></div>
    {operations && <Link to={`/projects/${row.id}/operations`} className="ui-link block">资产操作与作废记录</Link>}
  </div>
}

export function ProductDetails({ id }: { id: number }) {
  const operations = useAssetOperations()
  const query = useQuery({ queryKey: ['catalog-product', id], queryFn: () => getProduct(id) })
  if (query.isPending) return <LoadingState>正在读取商品…</LoadingState>
  if (query.isError) return <ErrorState retry={() => void query.refetch()}>{query.error.message}</ErrorState>
  const item = query.data
  return <div className="space-y-4">
    <FileThumbnail src={thumbUrl(item.id)} name={item.display_name} />
    <div><h3 className="break-words text-base font-semibold">{item.display_name}</h3><p className="mt-1 text-xs text-slate-500">{item.product_code} · {item.kind === 'set' ? '套装' : '单品'}</p></div>
    <div className="flex flex-wrap gap-2"><Link to={`/products/${item.id}`} className="ui-button ui-primary">打开商品</Link><CopyLink url={item.product_url} /></div>
    {operations && <Link to={`/projects/${item.project.id}/operations?product=${item.id}`} className="ui-link">商品操作</Link>}
    <dl className="space-y-2 border-t border-slate-200 pt-3">{item.tags.map(tag => <div key={tag.key} className="grid grid-cols-[5rem_minmax(0,1fr)] gap-2 text-xs">
      <dt className="text-slate-500">{tag.label}</dt><dd className="break-words">{tag.confirmation_status === 'confirmed_unknown' ? '已确认无法判断' : tag.value || '—'}</dd></div>)}</dl>
    {item.free_text && <section><h4 className="font-medium">自由描述</h4><p className="mt-1 whitespace-pre-wrap break-words text-slate-600">{item.free_text}</p></section>}
    <section><h4 className="font-medium">{item.kind === 'set' ? '包含的单品' : '所属套装'}</h4>
      {item.related.length ? <ul className="mt-2 space-y-1">{item.related.map(row => <li key={row.id}><Link to={`/products/${row.id}`} className="ui-link">{row.display_name}</Link></li>)}</ul> :
        <p className="mt-1 text-xs text-slate-500">无关联商品</p>}
    </section>
  </div>
}

export function FileDetails({ id, row, history }: { id: number; row?: FileRow; history?: string }) {
  const query = useQuery({ queryKey: ['catalog-file', id], queryFn: () => checkFile(id) })
  const [preview, setPreview] = useState(false)
  const [error, setError] = useState('')
  if (query.isPending) return <LoadingState>正在检查文件权限…</LoadingState>
  if (query.isError) return <ErrorState retry={() => void query.refetch()}>{query.error.message}</ErrorState>
  const file = query.data
  const load = () => {
    if (file.warning && !window.confirm(`${file.warning}\n仍要下载当前 NAS 文件？`)) return
    try { triggerDownload(file.url) } catch (reason) { setError(describeError(reason)) }
  }
  return <div className="space-y-3"><h3 className="break-words text-base font-semibold">{file.name}</h3>
    {row && <p className="break-all text-xs text-slate-500">{row.rel_path}</p>}
    <div className="flex flex-wrap gap-2"><Badge>{file.is_image ? '图片' : file.name.includes('.') ? file.name.split('.').at(-1)?.toUpperCase() : '文件'}</Badge>
      <Badge>{formatBytes(file.size)}</Badge><Badge tone={file.status === 'active' ? 'success' : 'warning'}>{file.status === 'active' ? '有效' : file.status === 'replaced' ? '历史文件' : file.status === 'voided' ? '已作废' : file.status}</Badge></div>
    {row && <p className="text-xs text-slate-500">修改时间 · {new Date(row.mtime).toLocaleString('zh-CN')}</p>}
    {history && <p className="break-words text-xs text-slate-500">{history}</p>}
    {file.warning && <p role="alert" className="rounded-lg bg-amber-50 p-3 text-amber-800">{file.warning}</p>}
    {error && <ErrorState>{error}</ErrorState>}
    <div className="flex flex-wrap items-center gap-2">
      <Button onClick={load}><Icon name="download" />下载文件</Button>
      {file.is_image && <Button primary={false} onClick={() => setPreview(true)}>预览图片</Button>}
      <CopyLink url={row?.link_url ?? null} />
      <Link className="ui-link" to={`/files/${id}`}>打开文件详情</Link>
    </div>
    <Dialog open={preview} title="文件预览" onClose={() => setPreview(false)}>{preview && <AssetPreview fileId={id} name={file.name} warning={file.warning} />}</Dialog>
  </div>
}
