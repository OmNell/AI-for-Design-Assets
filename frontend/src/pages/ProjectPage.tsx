import { useId, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getProject, getProjectFiles, thumbUrl, type FileRow, type ProjectDetail } from '../api/catalog'
import { classificationSearch, allClasses, DirectoryTree } from '../components/catalog/DirectoryTree'
import { FileDetails, ProductDetails, ProjectDetails, useAssetOperations } from '../components/catalog/AssetDetails'
import { Badge, Breadcrumb, Button, Checkbox, ContextPane, EmptyState, ErrorState, FileIcon, FileThumbnail, Icon, LoadingState, Panel, Select, Table, ViewSwitch } from '../components/ui'
import { CopyLink } from '../components/CopyLink'
import { formatBytes } from '../lib/terms'
import { describeError, downloadFile, sequentialDownload, type BatchTransfer } from '../lib/download'

type Mode = 'files' | 'products' | 'history'
type Sort = 'name' | 'size' | 'recent'
type Pick = { kind: 'project' } | { kind: 'file'; row: FileRow } | { kind: 'product'; id: number } |
  { kind: 'history'; row: ProjectDetail['history'][number] }
const tabs: { key: Mode; title: string }[] = [{ key: 'files', title: '全部文件' }, { key: 'products', title: '商品资产' }, { key: 'history', title: '历史版本' }]

function fileOrder(a: FileRow, b: FileRow, order: Sort) {
  return order === 'size' ? b.size - a.size : order === 'recent' ? b.mtime - a.mtime : a.name.localeCompare(b.name, 'zh-CN', { numeric: true })
}

function ProjectBrowser({ data }: { data: ProjectDetail }) {
  const id = data.project.id
  const navigate = useNavigate()
  const operations = useAssetOperations()
  const tabId = useId()
  const [mode, setMode] = useState<Mode>('files')
  const [views, setViews] = useState<Record<Exclude<Mode, 'history'>, 'list' | 'grid'>>({ files: 'list', products: 'grid' })
  const [folder, setFolder] = useState('')
  const [sort, setSort] = useState<Sort>('name')
  const [selected, setSelected] = useState<number[]>([])
  const [pick, setPick] = useState<Pick>()
  const [busy, setBusy] = useState(false)
  const [transfer, setTransfer] = useState<BatchTransfer>()
  const [error, setError] = useState('')
  const files = useQuery({ queryKey: ['project-files', id], queryFn: () => getProjectFiles(id), enabled: mode === 'files' })
  const productById = new Map(data.products_list.map(item => [item.id, item]))
  const path = folder ? folder.split('/') : []
  const prefix = folder ? folder + '/' : ''
  const listed = (files.data ?? []).flatMap(row => {
    if (!row.rel_path.startsWith(prefix)) return []
    const rest = row.rel_path.slice(prefix.length)
    return rest && !rest.includes('/') ? [row] : []
  }).sort((a, b) => fileOrder(a, b, sort))
  const childFolders = new Set(folder ? [] : data.folders.map(item => item.name))
  for (const row of files.data ?? []) {
    if (!row.rel_path.startsWith(prefix)) continue
    const rest = row.rel_path.slice(prefix.length)
    if (rest.includes('/')) childFolders.add(rest.split('/')[0])
  }
  const directories = [...childFolders].sort((a, b) => a.localeCompare(b, 'zh-CN', { numeric: true }))
  const visibleSelection = selected.filter(fileId => listed.some(row => row.id === fileId))
  const toggle = (fileId: number, checked: boolean) => setSelected(previous => checked ? [...new Set([...previous, fileId])] : previous.filter(value => value !== fileId))
  const enter = (next: string) => { setFolder(next); setSelected([]); setTransfer(undefined); setError(''); setPick(undefined) }
  const changeMode = (next: Mode) => { setMode(next); setPick(undefined); setSelected([]); setError(''); setTransfer(undefined) }
  const download = async (fileId?: number) => {
    setBusy(true); setError(''); setTransfer(undefined)
    try {
      if (fileId !== undefined) await downloadFile(fileId)
      else await sequentialDownload(visibleSelection, setTransfer)
    } catch (reason) { setError(describeError(reason)) }
    finally { setBusy(false) }
  }
  const contextTitle = pick?.kind === 'product' ? '商品详情' : pick?.kind === 'file' || pick?.kind === 'history' ? '文件详情' : '项目信息'
  return <div className="min-w-0 space-y-4">
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0"><Breadcrumb><Link to="/projects" className="ui-link">全部资产</Link><Icon name="right" />
        <button type="button" className="min-w-0 break-words text-left text-slate-700 hover:text-blue-700" onClick={() => setPick({ kind: 'project' })}>{data.project.project_code} · {data.project.company_name}</button>
      </Breadcrumb><h1 className="mt-2 break-words text-2xl font-semibold">{data.project.project_name}</h1></div>
      <div className="flex flex-wrap items-center gap-2"><Button primary={false} onClick={() => setPick({ kind: 'project' })}>项目信息</Button>
        <CopyLink url={data.project.link_url} label="复制项目链接" />
        {operations && <Link className="ui-button ui-secondary" to={`/projects/${id}/operations`}>资产操作与作废记录</Link>}</div>
    </header>
    <div className="browse-layout" data-context={Boolean(pick)}>
      <DirectoryTree value={allClasses} active={data.project} onChange={value => navigate({ pathname: '/projects', search: classificationSearch(value).toString() })} />
      <div className="min-w-0 space-y-4">
        <section className="ui-panel min-w-0 overflow-hidden">
          <div role="tablist" aria-label="项目内容" className="flex flex-wrap gap-1 border-b border-slate-200 px-3 pt-2">
            {tabs.map((tab, i) => <button type="button" role="tab" key={tab.key} id={`${tabId}-${tab.key}`} aria-selected={mode === tab.key}
              aria-controls={`${tabId}-content`} tabIndex={mode === tab.key ? 0 : -1} onClick={() => changeMode(tab.key)}
              onKeyDown={event => {
                const index = event.key === 'ArrowRight' ? (i + 1) % tabs.length : event.key === 'ArrowLeft' ? (i + tabs.length - 1) % tabs.length :
                  event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1
                if (index >= 0) { event.preventDefault(); changeMode(tabs[index].key); document.getElementById(`${tabId}-${tabs[index].key}`)?.focus() }
              }}
              className="border-b-2 border-transparent px-3 py-2 text-sm font-medium text-slate-500 aria-selected:border-blue-600 aria-selected:text-blue-700">{tab.title}</button>)}
          </div>
          <div role="tabpanel" id={`${tabId}-content`} aria-labelledby={`${tabId}-${mode}`} className="min-w-0">
            {mode === 'files' && <>
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 p-3">
                <Breadcrumb><button type="button" onClick={() => enter('')} className="ui-link">项目根目录</button>
                  {path.map((part, i) => <span key={i} className="flex min-w-0 items-center gap-2"><Icon name="right" /><button type="button" onClick={() => enter(path.slice(0, i + 1).join('/'))} className="min-w-0 break-all text-left hover:text-blue-700">{part}</button></span>)}
                </Breadcrumb><div className="flex flex-wrap items-center gap-2">
                  <Select aria-label="文件排序" className="!w-auto" value={sort} onChange={event => setSort(event.target.value as Sort)}><option value="name">文件名</option><option value="recent">修改时间</option><option value="size">大小</option></Select>
                  <ViewSwitch view={views.files} onChange={value => setViews({ ...views, files: value })} />
                  <Button disabled={busy || !visibleSelection.length} onClick={() => void download()}><Icon name="download" />下载所选文件{visibleSelection.length ? `（${visibleSelection.length}）` : ''}</Button>
                </div>
              </div>
              {error && <div className="px-3 py-2"><ErrorState>{error}</ErrorState></div>}
              {transfer && <div aria-live="polite" className="m-3 rounded-lg bg-slate-50 p-3 text-sm">
                <p>共 {transfer.total} 个 · 已发送 {transfer.sent} 个 · 失败 {transfer.failed.length} 个</p>
                {busy && transfer.current && <p className="break-words">正在处理 {transfer.current}</p>}
                {transfer.failed.map((row, i) => <p key={i} className="break-words text-red-800">{row.name} · {row.message}</p>)}
              </div>}
              {files.isPending ? <LoadingState>正在读取文件…</LoadingState> : files.isError ? <ErrorState retry={() => void files.refetch()}>{files.error.message}</ErrorState> :
                !listed.length && !directories.length ? <EmptyState title="此资料夹暂无文件" /> : <>
                {views.files === 'grid' && listed.length > 0 && <label className="m-3 flex items-center gap-2 text-sm"><Checkbox checked={visibleSelection.length === listed.length} onChange={event => setSelected(event.target.checked ? listed.map(row => row.id) : [])} />选择全部文件</label>}
                {views.files === 'list' ? <div className="asset-file-table"><Table label="项目文件"><thead><tr>
                  <th className="w-10">{listed.length > 0 && <Checkbox aria-label="选择全部文件" checked={visibleSelection.length === listed.length} onChange={event => setSelected(event.target.checked ? listed.map(row => row.id) : [])} />}</th>
                  <th>名称</th><th>大小</th><th data-file-time>修改时间</th><th>操作</th></tr></thead><tbody>
                  {directories.map(name => <tr key={name}><td /><td><button type="button" className="flex w-full min-w-0 items-center gap-2 text-left font-medium" onClick={() => enter(prefix + name)}><FileIcon folder /><span className="min-w-0 break-all">{name}</span></button></td><td>—</td><td data-file-time>—</td><td /></tr>)}
                  {listed.map(row => <tr key={row.id} className={pick?.kind === 'file' && pick.row.id === row.id ? 'bg-blue-50' : ''}
                    onClick={() => setPick({ kind: 'file', row })} onDoubleClick={() => navigate(`/files/${row.id}`)}>
                    <td onClick={event => event.stopPropagation()}><Checkbox aria-label={`选择 ${row.name}`} checked={selected.includes(row.id)} onChange={event => toggle(row.id, event.target.checked)} /></td>
                    <td className="max-w-[25rem]"><button type="button" onClick={() => setPick({ kind: 'file', row })} className="flex w-full min-w-0 items-center gap-2 text-left"><FileIcon name={row.name} /><span title={row.name} className="line-clamp-2 min-w-0 break-all font-medium">{row.name}</span></button></td>
                    <td className="whitespace-nowrap text-xs">{formatBytes(row.size)}</td><td data-file-time className="whitespace-nowrap text-xs text-slate-500">{new Date(row.mtime).toLocaleString('zh-CN')}</td>
                    <td className="whitespace-nowrap"><Link to={`/files/${row.id}`} className="ui-link mr-2">打开</Link><button type="button" disabled={busy} className="ui-link" onClick={event => { event.stopPropagation(); void download(row.id) }}>下载</button></td>
                  </tr>)}
                </tbody></Table></div> : <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,9.5rem),1fr))] gap-3 p-3">
                  {directories.map(name => <button type="button" key={name} className="min-w-0 rounded-lg border border-slate-200 p-3 text-left hover:bg-blue-50" onClick={() => enter(prefix + name)}>
                    <FileIcon folder /><span className="mt-2 block break-all">{name}</span></button>)}
                  {listed.map(row => <div key={row.id} className={`relative min-w-0 rounded-lg border p-2 ${pick?.kind === 'file' && pick.row.id === row.id ? 'border-blue-300 bg-blue-50' : 'border-slate-200'}`}>
                    <Checkbox aria-label={`选择 ${row.name}`} checked={selected.includes(row.id)} onChange={event => toggle(row.id, event.target.checked)} className="absolute left-4 top-4" />
                    <button type="button" className="w-full text-left" onClick={() => setPick({ kind: 'file', row })} onDoubleClick={() => navigate(`/files/${row.id}`)}>
                      <FileThumbnail name={row.name} /><span className="mt-2 block break-all font-medium">{row.name}</span><span className="text-xs text-slate-500">{formatBytes(row.size)}</span></button>
                    <Link to={`/files/${row.id}`} className="ui-link mt-1 block text-xs">打开详情</Link>
                  </div>)}
                </div>}
                </>}
            </>}
            {mode === 'products' && <>
              <div className="flex items-center justify-between gap-3 border-b border-slate-200 p-3"><span className="text-slate-500">共 {data.products_list.length} 件商品</span>
                <ViewSwitch view={views.products} onChange={value => setViews({ ...views, products: value })} /></div>
              {!data.products_list.length ? <EmptyState title="暂无有效商品" /> :
                <ul className={views.products === 'grid' ? 'grid grid-cols-[repeat(auto-fill,minmax(min(100%,10rem),1fr))] gap-3 p-3' : 'divide-y divide-slate-100'}>
                  {data.products_list.map(item => {
                    const members = item.kind === 'set' ? item.related.flatMap(id => {
                      const member = productById.get(id)
                      return member?.kind === 'item' ? [member] : []
                    }) : []
                    return <li key={item.id} className={`min-w-0 p-3 ${views.products === 'grid' ? 'rounded-lg border border-slate-200' : ''} ${pick?.kind === 'product' && pick.id === item.id ? 'bg-blue-50' : ''}`}>
                      <button type="button" onClick={() => setPick({ kind: 'product', id: item.id })} onDoubleClick={() => navigate(`/products/${item.id}`)} className={`w-full min-w-0 text-left ${views.products === 'list' ? 'flex items-center gap-3' : ''}`}>
                        <div className={views.products === 'list' ? 'w-14 shrink-0' : 'mb-2'}><FileThumbnail src={thumbUrl(item.id)} name={item.display_name} /></div>
                        <span className="min-w-0"><span className="block break-words font-medium">{item.display_name}</span><span className="text-xs text-slate-500">{item.product_code} · {item.kind === 'set' ? '套装' : '单品'}</span></span>
                      </button>
                      <div className="mt-2 flex flex-wrap gap-1">{item.tags.slice(0, 3).map((tag, index) => <Badge key={index}>{tag}</Badge>)}</div>
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                        <Link to={`/products/${item.id}`} className="ui-link">打开商品</Link>
                        <CopyLink url={item.link_url} label="复制商品链接" />
                      </div>
                      {item.kind === 'set' && <details className="mt-2 break-words text-xs">
                        <summary className="cursor-pointer">包含的单品（{members.length}）</summary>
                        {members.length ? <ul className="mt-2 space-y-1">{members.map(member => <li key={member.id}>
                          <Link to={`/products/${member.id}`} className="ui-link">{member.display_name}</Link>
                        </li>)}</ul> : <p className="mt-2 text-slate-500">无关联单品</p>}
                      </details>}
                    </li>
                  })}
                </ul>}
            </>}
            {mode === 'history' && (data.history.length ? <Table label="历史版本">
              <thead><tr><th>历史文件</th><th>变更原因</th><th>时间</th><th>操作</th></tr></thead><tbody>
                {data.history.map(row => <tr key={row.id} className={pick?.kind === 'history' && pick.row.id === row.id ? 'bg-blue-50' : ''}>
                  <td className="break-all"><button type="button" className="text-left font-medium" onClick={() => setPick({ kind: 'history', row })} onDoubleClick={() => navigate(`/files/${row.file_id}`)}>{row.old_rel_path}</button></td>
                  <td className="break-words">{row.reason}</td><td className="whitespace-nowrap">{new Date(row.created_at).toLocaleDateString('zh-CN')}</td>
                  <td><Link to={`/files/${row.file_id}`} className="ui-link whitespace-nowrap">打开</Link>
                    {operations && <Link to={`/projects/${id}/operations?file=${row.file_id}`} className="ui-link mt-1 block whitespace-nowrap text-xs">版本操作</Link>}</td>
                </tr>)}
              </tbody></Table> : <EmptyState title="暂无历史版本" />)}
          </div>
        </section>
        {data.logs.length > 0 && <Panel><details><summary className="cursor-pointer font-semibold">操作记录</summary>
          <ul className="mt-3 space-y-2 text-sm">{data.logs.map((row, index) => <li key={index}>{new Date(row.ts).toLocaleString('zh-CN')} · {row.user} · {row.action} · {row.result}</li>)}</ul></details></Panel>}
      </div>
      {pick && <ContextPane title={contextTitle} onClose={() => setPick(undefined)}>
        {pick.kind === 'project' ? <ProjectDetails row={data.project} /> : pick.kind === 'product' ? <ProductDetails id={pick.id} /> :
          pick.kind === 'file' ? <FileDetails key={pick.row.id} id={pick.row.id} row={pick.row} /> :
            <FileDetails key={pick.row.id} id={pick.row.file_id} history={`${pick.row.reason} · ${pick.row.old_rel_path}`} />}
      </ContextPane>}
    </div>
  </div>
}

export function ProjectPage() {
  const id = Number(useParams().id)
  const valid = Number.isSafeInteger(id) && id > 0
  const detail = useQuery({ queryKey: ['project', id], queryFn: () => getProject(id), enabled: valid })
  if (!valid) return <ErrorState>项目编号不正确</ErrorState>
  if (detail.isPending) return <LoadingState>正在读取项目…</LoadingState>
  if (detail.isError) return <Panel><ErrorState retry={() => void detail.refetch()}>{detail.error.message}</ErrorState><Link to="/projects" className="ui-link mt-3 inline-block">返回项目列表</Link></Panel>
  return <ProjectBrowser key={id} data={detail.data} />
}
