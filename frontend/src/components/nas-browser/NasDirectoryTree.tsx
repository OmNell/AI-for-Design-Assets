import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getNasDirectory } from '../../api/nasBrowser'
import { Button, ErrorState, FileIcon, LoadingState, Panel } from '../ui'

interface TreeProps {
  current: string
  onOpen: (path: string) => void
}
interface NodeProps extends TreeProps { path: string; name: string }

function DirectoryChildren({ path, current, onOpen }: Omit<NodeProps, 'name'>) {
  const [page, setPage] = useState(1)
  const result = useQuery({
    queryKey: ['nas-browser', path, 'folders', page],
    queryFn: ({ signal }) => getNasDirectory(path, { page, kind: 'folder', sort: 'name', q: '' }, signal),
  })
  if (result.isPending) return <LoadingState>正在读取子目录…</LoadingState>
  if (result.isError) return <ErrorState retry={() => void result.refetch()}>{result.error.message}</ErrorState>
  return <ul className="ml-2 space-y-1 border-l border-slate-200 pl-1">
    {result.data.folders.map(item => <TreeNode key={item.path} path={item.path} name={item.name} current={current} onOpen={onOpen} />)}
    {!result.data.total && <li className="px-2 text-xs text-slate-500">没有子目录</li>}
    {result.data.total > result.data.page_size && <li className="flex flex-wrap gap-1 py-1">
      <Button primary={false} disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button>
      <Button primary={false} disabled={page * result.data.page_size >= result.data.total} onClick={() => setPage(page + 1)}>下一页</Button>
    </li>}
  </ul>
}

function TreeNode({ path, name, current, onOpen }: NodeProps) {
  const [expanded, setExpanded] = useState(false)
  return <li>
    <div className={'flex min-w-0 items-center gap-1 rounded-md ' + (current === path ? 'bg-blue-50 text-blue-700' : '')}>
      <button type="button" className="shrink-0 px-1.5 py-2 text-slate-600" aria-expanded={expanded}
        aria-label={(expanded ? '折叠' : '展开') + name} onClick={() => setExpanded(!expanded)}>{expanded ? '−' : '+'}</button>
      <button type="button" className="flex min-w-0 items-center gap-1 py-1 pr-1 text-left text-sm"
        aria-current={current === path ? 'location' : undefined} onClick={() => onOpen(path)}>
        <FileIcon folder /><span className="break-all">{name}</span>
      </button>
    </div>
    {expanded && <DirectoryChildren path={path} current={current} onOpen={onOpen} />}
  </li>
}

export function NasDirectoryTree({ root, current, onOpen }: TreeProps & { root?: string }) {
  return <Panel className="min-w-0 !p-3">
    <nav aria-label="NAS 目录树">
      <h2 className="mb-3 font-semibold">NAS 目录</h2>
      {root ? <ul><TreeNode path="" name={root} current={current} onOpen={onOpen} /></ul> : null}
    </nav>
  </Panel>
}
