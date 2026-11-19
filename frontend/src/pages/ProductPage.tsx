import { Link, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getProduct } from '../api/catalog'
import { GlassPanel } from '../components/glass/GlassPanel'
import { CopyLink } from '../components/CopyLink'
import { AssetPreview } from '../components/AssetPreview'

export function ProductPage() {
  const id = Number(useParams().id)
  const valid = Number.isSafeInteger(id) && id > 0
  const query = useQuery({ queryKey: ['catalog-product', id], queryFn: () => getProduct(id), enabled: valid })
  if (!valid) return <GlassPanel><p role="alert">商品编号不正确</p></GlassPanel>
  if (query.isPending) return <GlassPanel><p role="status">正在读取商品…</p></GlassPanel>
  if (query.isError) return <GlassPanel><p role="alert">{query.error.message}</p><Link to="/projects" className="underline">返回项目列表</Link></GlassPanel>
  const product = query.data
  return <div className="grid grid-cols-[minmax(0,1fr)_440px] gap-5">
    <GlassPanel><AssetPreview key={product.file_id} fileId={product.file_id} name={product.display_name} /></GlassPanel>
    <GlassPanel>
      <p className="text-sm text-slate-600">{product.product_code} · {product.kind === 'set' ? '套装' : '单品'} · 有效</p>
      <h1 className="mt-2 break-all text-2xl font-semibold">{product.display_name}</h1>
      <Link to={`/projects/${product.project.id}`} className="mt-3 block text-sm text-emerald-800 underline">{product.project.project_code} {product.project.project_name}</Link>
      <div className="my-4 flex flex-wrap gap-2">
        <CopyLink url={product.product_url} label="复制商品链接" /><CopyLink url={product.project.link_url} label="复制项目链接" />
        <Link to={`/files/${product.file_id}`} className="self-center text-sm underline">查看主图文件</Link>
        <Link to={`/projects/${product.project.id}/operations?product=${product.id}`} className="self-center text-sm text-emerald-800 underline">商品操作</Link>
      </div>
      <h2 className="font-semibold">已确认标签</h2>
      <dl className="my-3 space-y-2 text-sm">{product.tags.map(tag => <div key={tag.key} className="grid grid-cols-[5.5rem_1fr] gap-2">
        <dt className="text-slate-500">{tag.label}</dt>
        <dd>{tag.confirmation_status === 'confirmed_unknown' ? '已确认无法判断' : tag.value || '—'}</dd></div>)}</dl>
      {product.free_text && <><h2 className="mt-5 font-semibold">自由描述</h2><p className="mt-2 whitespace-pre-wrap break-words text-sm">{product.free_text}</p></>}
      <h2 className="mt-5 font-semibold">{product.kind === 'set' ? '包含的单品' : '所属套装'}</h2>
      {product.related.length ? <ul className="mt-2 space-y-2 text-sm">{product.related.map(row =>
        <li key={row.id}><Link to={`/products/${row.id}`} className="text-emerald-800 underline">{row.product_code} {row.display_name}</Link></li>)}</ul> :
        <p className="mt-2 text-sm text-slate-500">无关联商品</p>}
    </GlassPanel>
  </div>
}
