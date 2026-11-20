import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { searchAssets, searchClick, searchModeLabels, type SearchFilters, type SearchHit, type SearchInput, type SearchMode, type SearchResult } from '../api/search'
import { getCatalogTree } from '../api/audit'
import { thumbUrl } from '../api/catalog'
import { ProductDetails } from '../components/catalog/AssetDetails'
import { Badge, Button, ContextPane, EmptyState, ErrorState, FileThumbnail, Icon, Input, Panel, Select, Textarea, ViewSwitch } from '../components/ui'

const initialFilters: SearchFilters = { won: null, industry: '', company: '' }

export function SearchPage() {
  const [params] = useSearchParams()
  const entry = params.get('q')?.trim().slice(0, 250) ?? ''
  const navigate = useNavigate()
  const location = useLocation()
  const [mode, setMode] = useState<SearchMode>('quick')
  const [text, setText] = useState(entry)
  const [filters, setFilters] = useState(initialFilters)
  const [input, setInput] = useState<SearchInput>()
  const [result, setResult] = useState<SearchResult>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<SearchHit>()
  const [view, setView] = useState<'list' | 'grid'>('grid')
  const sequence = useRef(0)
  const pending = useRef<AbortController>()
  const catalog = useQuery({ queryKey: ['catalog-tree'], queryFn: getCatalogTree })
  const industries = catalog.data?.[0]?.industries ?? []
  const chosen = industries.find(row => String(row.id) === filters.industry)
  const companies = chosen?.companies ?? industries.flatMap(row => row.companies)

  const run = useCallback(async (data: SearchInput) => {
    if (!data.query) return
    const revision = ++sequence.current
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setInput(data)
    setResult(undefined)
    setBusy(true)
    setError('')
    setSelected(undefined)
    try {
      const found = await searchAssets(data, controller.signal)
      if (revision !== sequence.current) return
      if (found.mode !== data.mode) throw new Error('返回的检索模式与请求不一致，请刷新工作台后重试')
      setResult(found)
    } catch (reason: unknown) {
      if (revision === sequence.current) {
        setResult(undefined)
        setError(reason instanceof Error ? reason.message : '搜索暂不可用，请稍后重试')
      }
    } finally {
      if (revision === sequence.current) {
        pending.current = undefined
        setBusy(false)
      }
    }
  }, [])
  useEffect(() => {
    if (!entry) return
    setText(entry); setFilters(initialFilters); setMode('quick')
    void run({ ...initialFilters, query: entry, mode: 'quick', remove: [] })
  }, [entry, location.key, run])
  useEffect(() => () => { sequence.current++; pending.current?.abort() }, [])
  const switchMode = (next: SearchMode) => {
    if (mode === next) return
    sequence.current++
    pending.current?.abort()
    pending.current = undefined
    setMode(next); setInput(undefined); setResult(undefined); setError(''); setBusy(false); setSelected(undefined)
  }
  const submit = () => void run({ ...filters, query: text.trim(), mode, remove: [] })
  const remove = (condition: string) => { if (input) void run({ ...input, remove: [...input.remove, condition] }) }
  const track = (kind: 'product' | 'project', id: number) => {
    if (result) void searchClick(result.search_id, kind, id).catch(() => undefined)
  }
  const chip = (key: string, label: string) => <button key={key} type="button" disabled={busy}
    aria-label={`移除 ${label}`} className="rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs hover:bg-blue-50 disabled:opacity-50"
    onClick={() => remove(key)}>{label} <span aria-hidden="true">×</span></button>

  return <div className="min-w-0 space-y-4">
    <header><h1 className="text-2xl font-semibold tracking-tight">智能检索</h1></header>
    <Panel>
      <div role="group" aria-label="检索模式" className="inline-flex flex-wrap gap-1 rounded-lg bg-slate-100 p-1">
        {(['quick', 'semantic'] as const).map(value => <button key={value} type="button"
          aria-pressed={mode === value} onClick={() => switchMode(value)}
          className={'rounded-md px-3 py-2 text-sm font-medium focus-visible:outline-blue-600 ' +
            (mode === value ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-600 hover:text-slate-900')}>
          {searchModeLabels[value]}
        </button>)}
      </div>
      <form className="mt-4 max-w-4xl space-y-3" onSubmit={event => { event.preventDefault(); submit() }}>
        <h2 className="text-lg font-semibold">{searchModeLabels[mode]}</h2>
        <p className="text-sm text-slate-600">{mode === 'quick'
          ? '通过商品名称、编号、品类或标签，快速查找已入库的设计资产。'
          : '使用自然语言描述你的产品需求，AI 将结合商品标签与语义进行匹配。'}</p>
        <div className="flex flex-wrap items-end gap-3 sm:flex-nowrap">
          {mode === 'quick'
            ? <Input type="search" aria-label="搜索词" maxLength={250} required
                placeholder="输入商品名称、项目编号或关键词，例如：陶瓷茶具" value={text} onChange={event => setText(event.target.value)} />
            : <Textarea aria-label="搜索词" maxLength={250} rows={3} required
                placeholder="例如：寻找适合银行高净值客户、具有中式文化元素、适合商务赠礼的产品"
                value={text} onChange={event => setText(event.target.value)} />}
          <Button type="submit" loading={busy} disabled={!text.trim()} className="shrink-0">
            {!busy && <Icon name="search" />}{busy ? '搜索中…' : mode === 'quick' ? '快速搜索' : '开始 AI 搜索'}
          </Button>
        </div>
        <p className="text-xs text-slate-500">{mode === 'quick' ? '本地匹配，不调用 AI。' : 'AI 搜索需要调用模型，响应时间可能较长。'}</p>
      </form>
    </Panel>
    <div className={'grid min-w-0 items-start gap-4 lg:grid-cols-[190px_minmax(0,1fr)] ' +
        (selected ? 'xl:grid-cols-[190px_minmax(0,1fr)_auto]' : '')}>
      <Panel className="min-w-0 space-y-4 !p-4 xl:sticky xl:top-0">
        <h2 className="font-semibold">筛选条件</h2>
        <label className="block space-y-1"><span className="text-xs text-slate-500">中标状态</span>
          <Select aria-label="中标状态" value={filters.won === null ? '' : String(filters.won)}
            onChange={event => setFilters({ ...filters, won: event.target.value === '' ? null : event.target.value === 'true' })}>
            <option value="">全部中标状态</option><option value="true">已中标</option><option value="false">未中标</option>
          </Select></label>
        <label className="block space-y-1"><span className="text-xs text-slate-500">行业</span>
          <Select aria-label="行业" value={filters.industry} onChange={event => setFilters({ ...filters, industry: event.target.value, company: '' })}>
            <option value="">全部行业</option>{industries.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
          </Select></label>
        <label className="block space-y-1"><span className="text-xs text-slate-500">企业</span>
          <Select aria-label="企业" value={filters.company} onChange={event => setFilters({ ...filters, company: event.target.value })}>
            <option value="">全部企业</option>{companies.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
          </Select></label>
        {catalog.isError && <p role="status" className="text-xs text-amber-800">分类暂不可用，仍可按搜索词检索。</p>}
      </Panel>
      <div className="min-w-0 space-y-4">
        {error && <ErrorState retry={input ? () => void run(input) : undefined}>{error}</ErrorState>}
        {!result && !error && <Panel><EmptyState title={busy ? '正在查找资产…' : '输入搜索词开始查找'} /></Panel>}
        {result && <>
          <Panel><div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold" aria-live="polite">商品 {result.products.length} 项 · 项目 {result.projects.length} 项</div>
            <div className="flex items-center gap-3"><Badge tone="info">{searchModeLabels[result.mode]}</Badge><ViewSwitch view={view} onChange={setView} /></div>
          </div>
            <div className="mt-3 flex flex-wrap gap-2" aria-label="本次查询条件">
              {Object.entries(result.conditions.fields).map(([key, words]) => chip(key, `${result.field_labels[key]} · ${words.join('、')}`))}
              {result.conditions.keywords.map(word => chip(`keyword:${word}`, word))}
              {result.conditions.exclude.map(word => chip(`exclude:${word}`, `排除 ${word}`))}
            </div>
            {result.degraded && <ul role="status" className="my-3 space-y-1 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">
              {result.mode === 'semantic' && <li className="font-medium">AI 搜索部分能力不可用</li>}
              {result.warnings.map(message => <li key={message}>{message}</li>)}</ul>}
            <div className="space-y-1 text-xs text-slate-500">
              {result.index_generation !== null && <p>正式索引第 {result.index_generation} 代</p>}
              {result.permission_filtered && <p>部分匹配资产因 NAS 权限或可用性未显示。</p>}
            </div>
          </Panel>
          {!!result.projects.length && <Panel><h2 className="font-semibold">匹配项目</h2>
            <ul className="mt-2 divide-y divide-slate-100">{result.projects.map(row => <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
              <div className="min-w-0"><Link to={`/projects/${row.id}`} onClick={() => track('project', row.id)} className="ui-link break-words">{row.project_code} · {row.project_name}</Link>
                <p className="mt-1 break-words text-xs text-slate-500">{row.company_name} · {row.industry_name} · {row.won ? '已中标' : '未中标'}</p>
                {row.reason && <p className="mt-1 text-xs text-slate-600">{row.reason}</p>}</div>
              {row.link_url && <a className="ui-link text-xs" href={row.link_url} onClick={() => track('project', row.id)}>永久链接</a>}</li>)}</ul>
          </Panel>}
          {!result.products.length ? <Panel><EmptyState title={result.exact && !result.projects.length
            ? '未找到可访问的编号，请核对后重试。' : '没有找到匹配商品。'} /></Panel> :
            <section aria-label="匹配商品" className={view === 'grid' ? 'grid grid-cols-[repeat(auto-fill,minmax(min(100%,13rem),1fr))] gap-3' : 'ui-panel divide-y divide-slate-100'}>
              {result.products.map(row => <article key={row.id} className={`min-w-0 p-3 ${view === 'grid' ? 'ui-panel' : 'flex gap-3'} ${selected?.id === row.id ? '!border-blue-300 !bg-blue-50' : ''}`}>
                <button type="button" className={`shrink-0 text-left ${view === 'list' ? 'w-16' : 'mb-2 w-full'}`} aria-label={`查看 ${row.name} 的匹配详情`}
                  onClick={() => { setSelected(row); track('product', row.id) }} onDoubleClick={() => navigate(`/products/${row.id}`)}>
                  <FileThumbnail src={thumbUrl(row.id)} name={row.name} />
                </button>
                <div className="min-w-0">
                  <button type="button" className="block break-words text-left font-semibold hover:text-blue-700" onClick={() => { setSelected(row); track('product', row.id) }}
                    onDoubleClick={() => navigate(`/products/${row.id}`)}>{row.name}</button>
                  <p className="mt-1 text-xs text-slate-500">{row.product_code} · {row.kind === 'set' ? '套装' : '单品'}</p>
                  <Link to={`/projects/${row.project_id}`} onClick={() => track('project', row.project_id)} className="ui-link mt-2 block break-words text-xs">{row.project_code} · {row.project_name}</Link>
                  <p className="mt-1 text-xs text-slate-500">{row.company_name} · {row.won ? '已中标' : '未中标'}</p>
                  <ul className="mt-2 flex flex-wrap gap-1">{Object.entries(row.tags).slice(0, 5).map(([key, value]) => <li key={key}><Badge>{result.field_labels[key]} · {value}</Badge></li>)}</ul>
                  {!!row.related.length && <p className="mt-2 break-words text-xs text-slate-500">{row.kind === 'set' ? '包含' : '所属套装'} · {row.related.join('、')}</p>}
                  <p className="mt-2 break-words text-sm">{row.reason}</p>
                  {!!row.matches.length && <p className="mt-1 text-xs text-slate-500">命中 · {row.matches.map(key => result.field_labels[key]).join('、')}</p>}
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Link to={`/products/${row.id}`} onClick={() => track('product', row.id)} className="ui-link text-xs">打开商品</Link>
                    {row.url && <a className="ui-link text-xs" href={row.url} onClick={() => track('product', row.id)}>商品永久链接</a>}
                  </div>
                </div>
              </article>)}
            </section>}
        </>}
      </div>
      {selected && <ContextPane title="匹配详情" onClose={() => setSelected(undefined)}>
        <div className="mb-4 rounded-lg bg-blue-50 p-3 text-blue-900"><h3 className="font-semibold">匹配理由</h3><p className="mt-1 text-xs">{selected.reason}</p></div>
        <ProductDetails id={selected.id} />
      </ContextPane>}
    </div>
  </div>
}
