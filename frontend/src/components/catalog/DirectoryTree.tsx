import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getCatalogTree } from '../../api/audit'
import { getProjects, type ProjectQuery, type ProjectRow } from '../../api/catalog'
import { getOperationOptions } from '../../api/operations'
import { ErrorState, Icon, LoadingState } from '../ui'

export type Classification = Pick<ProjectQuery, 'won' | 'industry' | 'company'>
export const allClasses: Classification = { won: '', industry: '', company: '' }

function Branch({ label, depth, open, selected, onExpand, onSelect }: { label: string; depth: number; open: boolean; selected: boolean; onExpand: () => void; onSelect: () => void }) {
  return <div className={`flex min-w-0 items-center rounded-md text-sm ${selected ? 'bg-blue-50 text-blue-700' : 'text-slate-600 hover:bg-slate-50'}`} style={{ paddingLeft: depth * 10 }}>
    <button type="button" aria-label={`${open ? '折叠' : '展开'}${label}`} aria-expanded={open} onClick={onExpand} className="grid size-7 shrink-0 place-items-center">
      <Icon name={open ? 'down' : 'right'} className="!size-3" />
    </button>
    <button type="button" aria-pressed={selected} title={label} onClick={onSelect} className="min-w-0 flex-1 truncate py-1.5 pr-2 text-left">{label}</button>
  </div>
}

function CompanyProjects({ filters, active }: { filters: Classification; active?: ProjectRow }) {
  const [page, setPage] = useState(1)
  const data = useQuery({ queryKey: ['directory-projects', filters, page], queryFn: () => getProjects({ ...filters, q: '', sort: 'name', page }) })
  if (data.isPending) return <LoadingState>读取项目…</LoadingState>
  if (data.isError) return <ErrorState retry={() => void data.refetch()}>{data.error.message}</ErrorState>
  const rows = active && !data.data.items.some(item => item.id === active.id) ? [active, ...data.data.items] : data.data.items
  return <div className="mb-1 ml-9 min-w-0 border-l border-slate-200 pl-1">
    {rows.map(project => <Link key={project.id} to={`/projects/${project.id}`} title={project.project_name}
      aria-current={project.id === active?.id ? 'page' : undefined}
      className="block truncate rounded-md px-2 py-1.5 text-xs text-slate-600 hover:text-blue-700 aria-[current=page]:bg-blue-50 aria-[current=page]:font-semibold aria-[current=page]:text-blue-700">{project.project_name}</Link>)}
    {!rows.length && <p className="px-2 py-1 text-xs text-slate-500">暂无项目</p>}
    {data.data.total > data.data.page_size && <div className="flex flex-wrap items-center gap-1 px-2 py-1 text-xs">
      <button className="ui-link disabled:opacity-50" type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>上页</button>
      <span>{page} / {Math.ceil(data.data.total / data.data.page_size)}</span>
      <button className="ui-link disabled:opacity-50" type="button" disabled={page * data.data.page_size >= data.data.total} onClick={() => setPage(page + 1)}>下页</button>
    </div>}
  </div>
}

export function DirectoryTree({ value, onChange, active }: { value: Classification; onChange: (value: Classification) => void; active?: ProjectRow }) {
  const tree = useQuery({ queryKey: ['catalog-tree'], queryFn: getCatalogTree })
  const [expanded, setExpanded] = useState<string[]>([])
  const activeRoot = tree.data?.find(item => item.won === active?.won)
  const directIndustry = activeRoot?.industries.find(item => item.name === active?.industry_name)
  const directCompany = directIndustry?.companies.find(item => item.name === active?.company_name)
  const details = useQuery({ queryKey: ['project-operations', active?.id], queryFn: () => getOperationOptions(active!.id),
    enabled: Boolean(active && activeRoot && !directCompany) })
  const identity = details.data?.before?.project
  const activeIndustry = activeRoot?.industries.find(item => item.id === identity?.industry_id) ?? directIndustry
  const activeCompany = activeIndustry?.companies.find(item => item.id === identity?.company_id) ?? directCompany
  const won = active ? String(active.won) : value.won
  const industry = active ? activeIndustry?.id : value.industry
  const company = active ? activeCompany?.id : value.company
  const current = company ? `${won}/${industry}/${company}` : industry ? `${won}/${industry}` : won
  useEffect(() => {
    if (!won) return
    const parents = [won, industry && `${won}/${industry}`, company && `${won}/${industry}/${company}`]
      .filter((item): item is string => typeof item === 'string' && !!item)
    setExpanded(keys => [...new Set([...keys, ...parents])])
  }, [won, industry, company])
  const expand = (key: string) => setExpanded(keys => keys.includes(key) ? keys.filter(k => k !== key) : [...keys, key])
  const select = (next: Classification, key: string) => { setExpanded(keys => keys.includes(key) ? keys : [...keys, key]); onChange(next) }
  return <nav aria-label="资产目录" className="ui-panel min-w-0 self-start overflow-hidden px-2 py-3 xl:sticky xl:top-0">
    <h2 className="px-2 pb-3 text-xs font-semibold text-slate-500">资产目录</h2>
    <button type="button" aria-pressed={!active && !value.won} onClick={() => onChange(allClasses)}
      className="mb-1 flex w-full items-center gap-2 rounded-md px-2 py-2 text-left font-medium text-slate-700 hover:bg-slate-50 aria-pressed:bg-blue-50 aria-pressed:text-blue-700"><Icon name="folder" />全部资产</button>
    <div className="max-h-[calc(100dvh-17rem)] min-h-0 overflow-y-auto">
      {tree.isPending ? <LoadingState /> : tree.isError ? <ErrorState retry={() => void tree.refetch()}>{tree.error.message}</ErrorState> :
        ([true, false] as const).map(won => {
          const root = tree.data.find(row => row.won === won)
          const key = String(won) as 'true' | 'false'
          return <div key={key}><Branch label={won ? '已中标' : '未中标'} depth={0} open={expanded.includes(key)} selected={current === key}
            onExpand={() => expand(key)} onSelect={() => select({ ...allClasses, won: key }, key)} />
            {expanded.includes(key) && root?.industries.map(industry => {
              const branch = `${key}/${industry.id}`, next = { ...allClasses, won: key, industry: String(industry.id) }
              return <div key={branch}><Branch label={industry.name} depth={1} open={expanded.includes(branch)} selected={current === branch}
                onExpand={() => expand(branch)} onSelect={() => select(next, branch)} />
                {expanded.includes(branch) && industry.companies.map(company => {
                  const leaf = `${branch}/${company.id}`, filters = { ...next, company: String(company.id) }
                  return <div key={leaf}><Branch label={company.name} depth={2} open={expanded.includes(leaf)} selected={current === leaf}
                    onExpand={() => expand(leaf)} onSelect={() => select(filters, leaf)} />
                    {expanded.includes(leaf) && <CompanyProjects filters={filters} active={current === leaf ? active : undefined} />}
                  </div>
                })}</div>
            })}</div>
        })}
    </div>
  </nav>
}

export function classificationSearch(value: Classification) {
  const query = new URLSearchParams()
  for (const key of ['won', 'industry', 'company'] as const) if (value[key]) query.set(key, value[key])
  return query
}
