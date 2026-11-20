import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { getProjects, type ProjectQuery } from '../api/catalog'
import { classificationSearch, DirectoryTree, type Classification } from '../components/catalog/DirectoryTree'
import { ProjectDetails } from '../components/catalog/AssetDetails'
import { Badge, Button, ContextPane, EmptyState, ErrorState, FileIcon, Input, LoadingState, Pagination, Select, Table, ViewSwitch } from '../components/ui'

export const statusNames: Record<string, string> = { active: '有效', voided: '已作废', deleted: '已删除', ingesting: '入库中' }

export function ProjectsPage({ embedded = false }: { embedded?: boolean }) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const won = params.get('won')
  const classification: Classification = { won: won === 'true' || won === 'false' ? won : '', industry: params.get('industry') ?? '', company: params.get('company') ?? '' }
  const [filters, setFilters] = useState({ q: '', page: 1, sort: 'newest' })
  const [text, setText] = useState('')
  const [picked, setPicked] = useState<number>()
  const [view, setView] = useState<'list' | 'grid'>('list')
  const query: ProjectQuery = { ...classification, ...filters }
  const list = useQuery({ queryKey: ['projects', query], queryFn: () => getProjects(query), placeholderData: keepPreviousData })
  const rows = list.isPlaceholderData ? [] : list.data?.items ?? []
  const selected = rows.find(item => item.id === picked)
  const update = (patch: Partial<typeof filters>) => { setFilters(current => ({ ...current, ...patch, page: patch.page ?? 1 })); setPicked(undefined) }
  const classify = (value: Classification) => { setParams(classificationSearch(value)); update({}) }

  return <div className="min-w-0 space-y-4">
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div>{!embedded && <h1 className="text-2xl font-semibold tracking-tight">全部资产</h1>}<p className="mt-1 text-slate-500">按中标状态、行业或企业浏览已入库项目。</p></div>
      <Link to="/projects/voided" className="ui-button ui-secondary">查看作废项目</Link>
    </header>
    <div className={`grid min-w-0 items-start gap-4 lg:grid-cols-[178px_minmax(0,1fr)] ${selected ? 'xl:grid-cols-[172px_minmax(0,1fr)_auto] 2xl:grid-cols-[204px_minmax(0,1fr)_auto]' : '2xl:grid-cols-[204px_minmax(0,1fr)]'}`}>
      <DirectoryTree value={classification} onChange={classify} />
      <section className="ui-panel min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 p-3">
          <form className="flex min-w-0 flex-1 flex-wrap items-center gap-2" onSubmit={event => { event.preventDefault(); update({ q: text.trim() }) }}>
            <Input aria-label="搜索项目" className="min-w-[10rem] flex-1" placeholder="立项编号 / 项目名 / 企业名" value={text} onChange={event => setText(event.target.value)} />
            <Button type="submit">查找</Button>
          </form>
          <div className="flex flex-wrap items-center gap-2"><Select aria-label="排序" className="!w-auto" value={filters.sort} onChange={event => update({ sort: event.target.value })}>
            <option value="newest">最近入库</option><option value="oldest">最早入库</option><option value="code">按编号</option><option value="name">按项目名</option>
          </Select><ViewSwitch view={view} onChange={setView} /></div>
        </div>
        {(list.isPending || list.isPlaceholderData) ? <LoadingState>正在加载项目…</LoadingState> : list.isError ? <ErrorState retry={() => void list.refetch()}>{list.error.message}</ErrorState> :
          !rows.length ? <EmptyState title={embedded && !filters.q && !classification.won && !classification.industry && !classification.company ? "暂无已入库项目" : "没有找到项目"} /> : view === 'list' ?
            <Table label="项目列表"><thead><tr><th>项目</th><th>企业</th><th>状态</th><th>文件</th><th>操作</th></tr></thead>
              <tbody>{rows.map(item => <tr key={item.id} className={picked === item.id ? 'bg-blue-50' : ''} onClick={() => setPicked(item.id)} onDoubleClick={() => navigate(`/projects/${item.id}`)}>
                <td className="min-w-0"><button type="button" className="flex w-full min-w-0 items-center gap-2 text-left" onClick={() => setPicked(item.id)}>
                  <FileIcon folder /><span className="min-w-0"><span className="block break-words font-medium">{item.project_name}</span><span className="text-xs text-slate-500">{item.project_code}</span></span>
                </button></td><td className="min-w-0 break-words text-slate-600">{item.company_name}<p className="text-xs text-slate-500">{item.industry_name}</p></td>
                <td><Badge tone={item.won ? 'success' : 'neutral'}>{item.won ? '已中标' : '未中标'}</Badge></td><td>{item.files}</td>
                <td><Link className="ui-link whitespace-nowrap" to={`/projects/${item.id}`}>打开</Link>
                  {item.link_url && <a href={item.link_url} className="ui-link mt-1 block whitespace-nowrap text-xs" aria-label={`打开 ${item.project_code} 的永久链接`}>永久链接</a>}</td>
              </tr>)}</tbody></Table> :
            <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,10rem),1fr))] gap-3 p-3">{rows.map(item =>
              <div key={item.id} className={`min-w-0 rounded-lg border p-3 ${picked === item.id ? 'border-blue-300 bg-blue-50' : 'border-slate-200'}`}>
                <button type="button" className="w-full text-left" onClick={() => setPicked(item.id)} onDoubleClick={() => navigate(`/projects/${item.id}`)}>
                  <FileIcon folder /><span className="mt-2 block break-words font-medium">{item.project_name}</span><span className="text-xs text-slate-500">{item.project_code}</span>
                </button><Link className="ui-link mt-2 block text-xs" to={`/projects/${item.id}`}>打开项目</Link>
              </div>)}</div>}
        {list.data && list.data.total > 0 && <Pagination total={list.data.total} page={filters.page} pages={Math.ceil(list.data.total / list.data.page_size)} onChange={page => update({ page })} />}
      </section>
      {selected && <ContextPane title="项目详情" onClose={() => setPicked(undefined)}><ProjectDetails row={selected} /></ContextPane>}
    </div>
  </div>
}
