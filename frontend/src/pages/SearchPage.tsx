import { useRef, useState } from 'react'
import { Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { searchAssets, searchClick, type SearchFilters, type SearchInput, type SearchResult } from '../api/search'
import { getCatalogTree } from '../api/audit'
import { thumbUrl } from '../api/catalog'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { GlassCard } from '../components/glass/GlassCard'

export function SearchPage() {
  const [text, setText] = useState('')
  const [filters, setFilters] = useState<SearchFilters>({ won: null, industry: '', company: '' })
  const [input, setInput] = useState<SearchInput>()
  const [result, setResult] = useState<SearchResult>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const sequence = useRef(0)
  const catalog = useQuery({ queryKey: ['catalog-tree'], queryFn: getCatalogTree })
  const industries = catalog.data?.[0]?.industries ?? []
  const chosen = industries.find(row => String(row.id) === filters.industry)
  const companies = chosen?.companies ?? industries.flatMap(row => row.companies)

  const run = async (data: SearchInput) => {
    if (!data.query) return
    const revision = ++sequence.current
    setInput(data)
    setBusy(true)
    setError('')
    try {
      const found = await searchAssets(data)
      if (revision === sequence.current) setResult(found)
    } catch (reason: unknown) {
      if (revision === sequence.current) {
        setResult(undefined)
        setError(reason instanceof Error ? reason.message : '搜索暂不可用，请稍后重试')
      }
    } finally {
      if (revision === sequence.current) setBusy(false)
    }
  }
  const submit = () => void run({ ...filters, query: text.trim(), remove: [] })
  const remove = (condition: string) => { if (input) void run({ ...input, remove: [...input.remove, condition] }) }
  const track = (kind: 'product' | 'project', id: number) => {
    if (result) void searchClick(result.search_id, kind, id).catch(() => undefined)
  }
  const chip = (key: string, label: string) => <button key={key} type="button" disabled={busy}
    aria-label={`移除 ${label}`} className="rounded-full border border-white/70 bg-white/55 px-3 py-1 text-sm disabled:opacity-50"
    onClick={() => remove(key)}>{label} <span aria-hidden="true">×</span></button>

  return <div className="space-y-5">
    <GlassPanel><h1 className="text-3xl font-semibold">搜索设计资产</h1>
      <form className="mt-5" onSubmit={event => { event.preventDefault(); submit() }}>
        <div className="flex gap-3">
          <input type="search" aria-label="搜索词" className="field flex-1" maxLength={250} required
            placeholder="输入编号、商品名，或描述要找的设计" value={text} onChange={event => setText(event.target.value)} />
          <GlassButton type="submit" disabled={busy || !text.trim()}>{busy ? '搜索中…' : '搜索'}</GlassButton>
        </div>
        <div className="mt-4 flex gap-3">
          <select aria-label="中标状态" className="field !w-44 !py-2 text-sm" value={filters.won === null ? '' : String(filters.won)}
            onChange={event => setFilters({ ...filters, won: event.target.value === '' ? null : event.target.value === 'true' })}>
            <option value="">全部中标状态</option><option value="true">已中标</option><option value="false">未中标</option>
          </select>
          <select aria-label="行业" className="field !w-44 !py-2 text-sm" value={filters.industry}
            onChange={event => setFilters({ ...filters, industry: event.target.value, company: '' })}>
            <option value="">全部行业</option>{industries.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
          </select>
          <select aria-label="企业" className="field !w-44 !py-2 text-sm" value={filters.company}
            onChange={event => setFilters({ ...filters, company: event.target.value })}>
            <option value="">全部企业</option>{companies.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
          </select>
        </div>
      </form>
    </GlassPanel>
    {error && <GlassPanel><p role="alert" className="text-red-800">{error}</p></GlassPanel>}
    {result && <>
      <GlassPanel>
        <div className="flex flex-wrap gap-2" aria-label="本次查询条件">
          {Object.entries(result.conditions.fields).map(([key, words]) => chip(key, `${result.field_labels[key]} · ${words.join('、')}`))}
          {result.conditions.keywords.map(word => chip(`keyword:${word}`, word))}
          {result.conditions.exclude.map(word => chip(`exclude:${word}`, `排除 ${word}`))}
        </div>
        {result.degraded && <ul role="status" className="my-3 space-y-1 rounded-xl bg-amber-100/80 p-3 text-sm text-amber-950">
          {result.warnings.map(message => <li key={message}>{message}</li>)}</ul>}
        <div className="mt-3 text-sm text-slate-600" aria-live="polite">
          商品 {result.products.length} 项 · 项目 {result.projects.length} 项
          {result.index_generation !== null && ` · 正式索引第 ${result.index_generation} 代`}
        </div>
        {result.permission_filtered && <p className="mt-2 text-sm text-slate-600">部分匹配资产因 NAS 权限或可用性未显示。</p>}
      </GlassPanel>
      {!!result.projects.length && <GlassPanel><h2 className="text-xl font-semibold">匹配项目</h2>
        <ul className="mt-3 space-y-3">{result.projects.map(row => <li key={row.id} className="flex items-center justify-between border-b border-slate-400/20 pb-3 text-sm">
          <Link to={`/projects/${row.id}`} onClick={() => track('project', row.id)} className="font-medium text-emerald-900 underline">
            {row.project_code} · {row.project_name}</Link>
          <span className="text-slate-600">{row.company_name} · {row.industry_name} · {row.won ? '已中标' : '未中标'}</span>
          {row.link_url && <a className="underline" href={row.link_url} onClick={() => track('project', row.id)}>永久链接</a>}</li>)}</ul>
      </GlassPanel>}
      {!result.products.length ? <GlassPanel><p className="text-slate-600">{result.exact && !result.projects.length
        ? '未找到可访问的编号，请核对后重试。' : '没有找到匹配商品。'}</p></GlassPanel> :
        <section aria-label="匹配商品" className="grid grid-cols-3 gap-4">{result.products.map(row => <GlassCard key={row.id}>
          <Link to={`/products/${row.id}`} onClick={() => track('product', row.id)} className="block">
            <div className="grid aspect-[4/3] place-items-center overflow-hidden rounded-xl bg-white/40">
              <img src={thumbUrl(row.id)} alt={row.name} loading="lazy" className="h-full w-full object-contain" /></div>
            <h2 className="mt-3 font-semibold text-emerald-900">{row.name}</h2></Link>
          <p className="mt-1 text-xs text-slate-600">{row.product_code} · {row.kind === 'set' ? '套装' : '单品'}</p>
          <Link to={`/projects/${row.project_id}`} className="mt-2 block text-sm text-emerald-800 underline">{row.project_code} · {row.project_name}</Link>
          <p className="mt-1 text-xs text-slate-600">{row.company_name} · {row.won ? '已中标' : '未中标'}</p>
          <ul className="mt-2 flex flex-wrap gap-1">{Object.entries(row.tags).slice(0, 5).map(([key, value]) =>
            <li key={key} className="rounded-full bg-white/70 px-2 py-0.5 text-xs">{result.field_labels[key]} · {value}</li>)}</ul>
          {!!row.related.length && <p className="mt-2 text-xs text-slate-600">{row.kind === 'set' ? '包含' : '所属套装'} · {row.related.join('、')}</p>}
          <p className="mt-2 text-sm">{row.reason}</p>
          {!!row.matches.length && <p className="mt-1 text-xs text-slate-600">命中 · {row.matches.map(key => result.field_labels[key]).join('、')}</p>}
          {row.url && <a className="mt-2 inline-block text-xs text-emerald-900 underline" href={row.url} onClick={() => track('product', row.id)}>商品永久链接</a>}
        </GlassCard>)}</section>}
    </>}
  </div>
}
