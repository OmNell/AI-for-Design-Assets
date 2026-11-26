import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getNasDirectory, type BrowseFilters, type BrowseKind, type BrowseSort } from '../api/nasBrowser'
import { NasDirectoryTree } from '../components/nas-browser/NasDirectoryTree'
import { NasFileList } from '../components/nas-browser/NasFileList'
import { NasFileDetails } from '../components/nas-browser/NasFileDetails'
import { Breadcrumb, Button, EmptyState, ErrorState, Input, LoadingState, Pagination, Select, ViewSwitch } from '../components/ui'

const initial: BrowseFilters = { page: 1, q: '', kind: 'all', sort: 'name' }

export function NasBrowserPage() {
  const [path, setPath] = useState('')
  const [picked, setPicked] = useState<string>()
  const [view, setView] = useState<'list' | 'grid'>('list')
  const [filters, setFilters] = useState(initial)
  const [search, setSearch] = useState('')
  const [root, setRoot] = useState<string>()
  const query = useQueryClient()
  const directory = useQuery({
    queryKey: ['nas-browser', path, filters],
    queryFn: ({ signal }) => getNasDirectory(path, filters, signal),
  })
  const data = directory.data
  useEffect(() => { if (data) setRoot(data.root_name) }, [data])
  const open = (target: string) => {
    if (target === path) return
    setPath(target); setPicked(undefined); setSearch('')
    setFilters(filter => ({ ...filter, page: 1, q: '', kind: 'all' }))
  }
  const change = (patch: Partial<BrowseFilters>) => {
    setFilters(filter => ({ ...filter, page: 1, ...patch })); setPicked(undefined)
  }
  const segments = path ? path.split('/') : []
  const refresh = () => { void query.invalidateQueries({ queryKey: ['nas-browser', path] }) }
  return <div className="browse-layout" data-context={Boolean(picked)}>
    <NasDirectoryTree root={root} current={path} onOpen={open} />
    <section className="ui-panel min-w-0 overflow-hidden" aria-label="NAS 文件浏览">
      <div className="space-y-3 border-b border-slate-200 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Breadcrumb>
            <button type="button" className="ui-link" onClick={() => open('')}>{root ?? '工作目录'}</button>
            {segments.map((part, index) => <span key={index} className="flex min-w-0 items-center gap-2">
              <span aria-hidden="true">/</span>
              <button type="button" className="ui-link break-all" aria-current={index === segments.length - 1 ? 'location' : undefined}
                onClick={() => open(segments.slice(0, index + 1).join('/'))}>{part}</button>
            </span>)}
          </Breadcrumb>
          <div className="flex flex-wrap gap-2">
            <Button primary={false} disabled={!segments.length} onClick={() => open(segments.slice(0, -1).join('/'))}>返回上级</Button>
            <Button primary={false} loading={directory.isFetching} onClick={refresh}>刷新</Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <form className="flex min-w-0 flex-1 gap-2" onSubmit={event => { event.preventDefault(); change({ q: search.trim() }) }}>
            <Input aria-label="搜索当前目录文件名" placeholder="搜索当前目录文件名" value={search} maxLength={255}
              onChange={event => setSearch(event.target.value)} />
            <Button type="submit">查找</Button>
          </form>
          <Select aria-label="文件类型" className="!w-auto" value={filters.kind} onChange={event => change({ kind: event.target.value as BrowseKind })}>
            <option value="all">全部类型</option><option value="folder">文件夹</option><option value="image">图片</option><option value="file">其他文件</option>
          </Select>
          <Select aria-label="文件排序" className="!w-auto" value={filters.sort} onChange={event => change({ sort: event.target.value as BrowseSort })}>
            <option value="name">按名称</option><option value="size">大小降序</option><option value="modified">最新修改</option>
          </Select>
          <ViewSwitch view={view} onChange={setView} />
        </div>
      </div>
      {directory.isPending ? <LoadingState>正在读取 NAS 目录…</LoadingState> :
        directory.isError ? <div className="p-3"><ErrorState retry={() => void directory.refetch()}>{directory.error.message}</ErrorState></div> :
          data && (data.total ? <>
            <NasFileList entries={[...data.folders, ...data.files]} selected={picked} view={view} onSelect={setPicked} onOpen={open} />
            <Pagination total={data.total} page={filters.page} pages={Math.ceil(data.total / data.page_size)} onChange={page => change({ page })} />
          </> : <EmptyState title={filters.q || filters.kind !== 'all' ? '没有找到匹配项' : '当前目录为空'} />)}
    </section>
    {picked && data && <NasFileDetails key={picked} root={data.root_name} path={picked} onClose={() => setPicked(undefined)} onOpen={open} />}
  </div>
}
