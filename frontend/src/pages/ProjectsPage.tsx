import { useState } from 'react'
import { Link } from 'react-router'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { getProjects, type ProjectQuery } from '../api/catalog'
import { getCatalogTree } from '../api/audit'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'

export const statusNames: Record<string, string> = { active: '有效', voided: '已作废', deleted: '已删除', ingesting: '入库中' }

export function ProjectsPage() {
  const [query, setQuery] = useState<ProjectQuery>({ q: '', won: '', industry: '', company: '', page: 1, sort: 'newest' })
  const [text, setText] = useState('')
  const tree = useQuery({ queryKey: ['catalog-tree'], queryFn: getCatalogTree, retry: false })
  const list = useQuery({ queryKey: ['projects', query], queryFn: () => getProjects(query), retry: false, placeholderData: keepPreviousData })
  const industries = tree.data?.[0]?.industries ?? []
  const companies = industries.find(item => String(item.id) === query.industry)?.companies ?? industries.flatMap(item => item.companies)
  const update = (patch: Partial<ProjectQuery>) => setQuery(current => ({ ...current, ...patch, page: patch.page ?? 1 }))
  const pages = list.data ? Math.max(1, Math.ceil(list.data.total / list.data.page_size)) : 1
  return <GlassPanel><h1 className="text-2xl font-semibold">项目</h1><p className="mt-3 text-sm text-slate-500">已入库的项目。按立项编号、项目名或企业名查找；下载时使用你本人的 NAS 权限。</p>
    <form className="mt-6 flex flex-wrap items-center gap-3" onSubmit={event => { event.preventDefault(); update({ q: text }) }}>
      <input aria-label="搜索项目" className="field max-w-sm !py-2.5 text-sm" placeholder="立项编号 / 项目名 / 企业名" value={text} onChange={event => setText(event.target.value)} />
      <select aria-label="中标状态" className="field !w-36 !py-2.5 text-sm" value={query.won} onChange={event => update({ won: event.target.value as ProjectQuery['won'] })}>
        <option value="">全部状态</option><option value="true">已中标</option><option value="false">未中标</option></select>
      <select aria-label="行业" className="field !w-40 !py-2.5 text-sm" value={query.industry} onChange={event => update({ industry: event.target.value, company: '' })}>
        <option value="">全部行业</option>{industries.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <select aria-label="企业" className="field !w-44 !py-2.5 text-sm" value={query.company} onChange={event => update({ company: event.target.value })}>
        <option value="">全部企业</option>{companies.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
      <select aria-label="排序" className="field !w-36 !py-2.5 text-sm" value={query.sort} onChange={event => update({ sort: event.target.value })}>
        <option value="newest">最近入库</option><option value="oldest">最早入库</option><option value="code">按编号</option><option value="name">按项目名</option></select>
      <GlassButton type="submit">查找</GlassButton></form>
    <div className="mt-6">{list.isPending ? <p>正在加载项目…</p> : list.isError ? <p role="alert" className="text-red-800">{list.error.message}</p> : !list.data.items.length ? <p className="text-slate-500">没有找到项目。</p> :
      <table className="w-full text-left text-sm"><thead className="text-xs text-slate-500"><tr><th className="p-3">立项编号</th><th className="p-3">项目名</th><th className="p-3">企业</th><th className="p-3">行业</th><th className="p-3">中标</th><th className="p-3">商品</th><th className="p-3">文件</th><th className="p-3">入库时间</th><th className="p-3">状态</th></tr></thead>
        <tbody>{list.data.items.map(item => <tr key={item.id} className="border-t border-slate-500/10">
          <td className="p-3"><Link to={`/projects/${item.id}`} className="font-medium text-emerald-800 underline underline-offset-4">{item.project_code}</Link></td>
          <td className="p-3">{item.project_name}</td><td className="p-3">{item.company_name}</td><td className="p-3">{item.industry_name || '—'}</td><td className="p-3">{item.won ? '是' : '否'}</td>
          <td className="p-3">{item.products}</td><td className="p-3">{item.files}</td><td className="p-3">{item.ingested_at ? new Date(item.ingested_at).toLocaleDateString('zh-CN') : '—'}</td><td className="p-3">{statusNames[item.status] ?? item.status}{item.link_url &&
            <a href={item.link_url} className="ml-2 text-emerald-800 underline" aria-label={`打开 ${item.project_code} 的永久链接`}>永久链接</a>}</td></tr>)}</tbody></table>}</div>
    {list.data && list.data.total > 0 && <div className="mt-5 flex items-center gap-4 text-sm text-slate-600"><span>共 {list.data.total} 个项目 · 第 {query.page} / {pages} 页</span>
      <GlassButton primary={false} disabled={query.page <= 1} onClick={() => update({ page: query.page - 1 })}>上一页</GlassButton>
      <GlassButton primary={false} disabled={query.page >= pages} onClick={() => update({ page: query.page + 1 })}>下一页</GlassButton></div>}
  </GlassPanel>
}
