import { useState } from 'react'
import { Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { recordClick, search, type Condition, type SearchFilters, type SearchResponse, type SearchResult } from '../api/search'
import { getCatalogTree } from '../api/audit'
import { thumbUrl } from '../api/catalog'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassSurface } from '../components/glass/GlassSurface'
import { GlassButton } from '../components/glass/GlassButton'

const KEY_TAGS = ['category', 'material', 'color', 'festival', 'audience']
const chipStyles: Record<Condition['type'], string> = { field: 'bg-white/70', other: 'bg-white/70', exclude: 'bg-red-100 text-red-900', company: 'bg-emerald-100 text-emerald-900' }

function Card({ item, logId, highlight }: { item: SearchResult; logId: number; highlight: boolean }) {
  const tags = [...item.tags].sort((a, b) => (KEY_TAGS.indexOf(a.key) + 99) % 99 - (KEY_TAGS.indexOf(b.key) + 99) % 99).slice(0, 5)
  return <article data-result={item.product_code} className={`rounded-2xl border p-3 ${highlight ? 'border-white/80 bg-white/45' : 'border-white/60 bg-white/25'}`}>
    <Link to={`/products/${item.id}`} onClick={() => { void recordClick(logId, item.id).catch(() => undefined) }} className="block">
      <img src={thumbUrl(item.id, 300)} alt={`${item.display_name} 缩略图`} loading="lazy" className="aspect-square w-full rounded-xl bg-white/40 object-contain" />
      <h3 className="mt-2 truncate text-sm font-medium" title={item.display_name}>{item.display_name}</h3></Link>
    <p className="mt-1 text-xs text-slate-500">{item.product_code} · {item.kind === 'set' ? '套装' : '单品'}</p>
    <ul className="mt-2 flex flex-wrap gap-1">{tags.map(tag => <li key={tag.key} title={tag.label} className="max-w-full truncate rounded-full bg-white/60 px-2 py-0.5 text-[11px]">{tag.value}</li>)}</ul>
    {!!item.related.length && <p className="mt-2 truncate text-xs text-slate-500" title={item.related.map(value => value.display_name).join('、')}>{item.kind === 'set' ? '包含' : '所属套装'}：{item.related.map(value => value.display_name).join('、')}</p>}
    <p className="mt-2 truncate text-xs"><Link to={`/projects/${item.project.id}`} className="text-emerald-800 underline underline-offset-4">{item.project.project_code} {item.project.company_name}</Link>
      <span className="ml-1 text-slate-500">{item.project.won ? '已中标' : '未中标'}</span></p>
    {item.reason && <p className="mt-2 rounded-lg bg-emerald-50/80 px-2 py-1 text-xs text-emerald-900">{item.reason}</p>}
    {!item.reason && <p className="mt-2 text-[11px] text-slate-400">{item.matched.length ? `命中：${item.matched.join('、')}` : item.by_vector ? '语义相近' : ''}</p>}
  </article>
}

export function SearchPage() {
  const [text, setText] = useState('')
  const [filters, setFilters] = useState<{ won: string; industry: string; company: string }>({ won: '', industry: '', company: '' })
  const [data, setData] = useState<SearchResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const tree = useQuery({ queryKey: ['catalog-tree'], queryFn: getCatalogTree, retry: false })
  const industries = tree.data?.[0]?.industries ?? []
  const companies = industries.find(item => String(item.id) === filters.industry)?.companies ?? industries.flatMap(item => item.companies)
  const run = async (query: string, conditions?: Condition[]) => {
    if (!query.trim()) return
    setBusy(true); setError('')
    const sent: SearchFilters = { won: filters.won === '' ? null : filters.won === 'true', industry: filters.industry ? Number(filters.industry) : null, company: filters.company ? Number(filters.company) : null }
    try { setData(await search(query.trim(), sent, conditions)) } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '搜索没有完成，请重试') } finally { setBusy(false) }
  }
  // 删除一个条件：把剩下的条件原样带回去重新搜索，不再让模型重新理解。
  const remove = (index: number) => { if (data) void run(data.query, data.conditions.filter((_, position) => position !== index)) }
  const top = data?.results.slice(0, 10) ?? []
  const rest = data?.results.slice(10) ?? []
  return <div className="space-y-5">
    <GlassPanel><h1 className="text-2xl font-semibold">搜索</h1><p className="mt-2 text-sm text-slate-500">说一句话，或直接输入立项编号、商品编号。例如「过年送领导的小摆件」「红色陶瓷保温杯，不要塑料」。</p>
      <form className="mt-5 flex gap-3" onSubmit={event => { event.preventDefault(); void run(text) }}>
        <input aria-label="搜索内容" autoFocus className="field !rounded-full !px-6 !py-4 text-lg" placeholder="想找什么？" maxLength={200} value={text} onChange={event => setText(event.target.value)} />
        <GlassButton type="submit" disabled={busy || !text.trim()} className="shrink-0">{busy ? '正在搜索…' : '搜索'}</GlassButton></form>
      <div className="mt-4 flex flex-wrap items-center gap-3 text-sm"><span className="text-slate-500">筛选（可选）</span>
        <select aria-label="中标状态" className="field !w-32 !py-2 text-sm" value={filters.won} onChange={event => setFilters({ ...filters, won: event.target.value })}><option value="">全部状态</option><option value="true">已中标</option><option value="false">未中标</option></select>
        <select aria-label="行业" className="field !w-40 !py-2 text-sm" value={filters.industry} onChange={event => setFilters({ ...filters, industry: event.target.value, company: '' })}><option value="">全部行业</option>{industries.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
        <select aria-label="企业" className="field !w-44 !py-2 text-sm" value={filters.company} onChange={event => setFilters({ ...filters, company: event.target.value })}><option value="">全部企业</option>{companies.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
      {error && <p role="alert" className="mt-4 text-sm text-red-800">{error}</p>}
    </GlassPanel>

    {data && <GlassSurface radius={24} className="px-6 py-4">
      {data.kind === 'code' ? <p data-testid="understood" className="text-sm">按编号「{data.query}」精确查找，没有调用模型。</p> :
        <div data-testid="understood"><p className="text-sm text-slate-600">系统理解的条件{data.from_cache ? '（来自缓存）' : ''}——点 × 可以去掉某个条件后重新搜索：</p>
          <ul className="mt-2 flex flex-wrap gap-2">{data.conditions.map((item, index) => <li key={`${item.type}:${item.field}:${item.terms[0]}`} className={`flex items-center gap-2 rounded-full py-1 pl-3 pr-1.5 text-sm ${chipStyles[item.type]}`}>
            <span><span className="text-xs text-slate-500">{item.label}：</span>{item.terms[0]}{item.terms.length > 1 && <span className="ml-1 text-xs text-slate-500">（也找：{item.terms.slice(1).join('、')}）</span>}</span>
            <button type="button" aria-label={`去掉条件 ${item.label}：${item.terms[0]}`} disabled={busy} className="h-5 w-5 rounded-full leading-none text-slate-500 hover:bg-white" onClick={() => remove(index)}>×</button></li>)}
            {!data.conditions.length && <li className="text-sm text-slate-500">没有识别出明确的条件，按语义相近查找。</li>}</ul></div>}
      {data.degraded && <p role="status" data-testid="degraded" className="mt-3 rounded-xl bg-amber-100/80 px-3 py-2 text-sm text-amber-900">本次搜索已降级：{data.notes.join('；')}。结果仍可参考，稍后可以再试一次。</p>}
      {data.relaxed && <p className="mt-2 text-xs text-slate-500">完全符合条件的结果较少，已放宽为在所有标签和描述里查找。</p>}
      <p className="mt-2 text-xs text-slate-400">共 {data.total} 个候选 · 用时 {data.elapsed_ms} 毫秒 · 模型调用 {data.model_calls} 次</p>
    </GlassSurface>}

    {data && !!data.project_hits.length && <GlassPanel><h2 className="text-lg font-semibold">项目命中</h2>
      <ul data-testid="project-hits" className="mt-3 flex flex-wrap gap-3">{data.project_hits.map(item => <li key={item.id}><Link to={`/projects/${item.id}`} className="block rounded-2xl border border-white/70 bg-white/40 px-4 py-2.5 text-sm hover:bg-white/70">
        <span className="font-medium">{item.project_code}</span> {item.company_name} {item.project_name}<span className="ml-2 text-xs text-slate-500">{item.won ? '已中标' : '未中标'}</span></Link></li>)}</ul></GlassPanel>}

    {data && <GlassPanel><h2 className="text-lg font-semibold">商品 {data.total ? `（最相关的 ${top.length} 个）` : ''}</h2>
      {!data.results.length ? <p data-testid="empty" className="mt-3 text-sm text-slate-500">没有找到相关的商品。可以换一种说法，去掉一些条件，或在上方「项目命中」里按项目查找。</p> :
        <div data-testid="results" className="mt-4 grid grid-cols-5 gap-4">{top.map(item => <Card key={item.id} item={item} logId={data.log_id} highlight />)}</div>}
      {!!rest.length && <details className="mt-6"><summary className="cursor-pointer text-sm text-slate-600">其余 {rest.length} 个候选（相关度较低）</summary>
        <div data-testid="more-results" className="mt-4 grid grid-cols-5 gap-4">{rest.map(item => <Card key={item.id} item={item} logId={data.log_id} highlight={false} />)}</div></details>}
    </GlassPanel>}
  </div>
}
