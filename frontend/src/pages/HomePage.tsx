import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { getBatches } from '../api/upload'
import { operationHref, operationLabels } from '../api/operations'
import { getHealth } from '../api/health'
import { getTodos, type Todo } from '../api/audit'
import { getProjects } from '../api/catalog'
import { roleLabels } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { Badge, ContextPane, EmptyState, ErrorState, Icon, LoadingState, Panel, Table } from '../components/ui'
import { canAccess, navigation } from '../app/navigation'

function TodoLink({ item }: { item: Todo }) {
  const count = <Badge tone={item.count ? 'info' : 'neutral'}>{item.count}</Badge>
  const style = 'flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm hover:border-blue-200 hover:bg-blue-50'
  if (item.link !== '/') return <Link to={item.link} className={style}>{item.label}{count}</Link>
  return <details><summary className={style}>{item.label}{count}</summary>
    <ul className="space-y-1 px-2 py-2">{item.batches.map(batch => {
      const path = (item.key === 'overdue' || item.key === 'ingest') ? `/review/${batch.id}` : `/batches/${batch.id}`
      return <li key={batch.id}><Link to={path} className="ui-link break-words">{batch.label}</Link></li>
    })}</ul></details>
}

function useTodos() {
  return useQuery({ queryKey: ['todos'], queryFn: getTodos, refetchInterval: 30000 })
}

function Todos({ data }: { data: ReturnType<typeof useTodos> }) {
  if (data.isPending) return <LoadingState>正在读取待办…</LoadingState>
  if (data.isError) return <ErrorState retry={() => void data.refetch()}>{data.error.message}</ErrorState>
  return data.data.length ? <nav aria-label="待办" data-testid="todos" className="grid gap-2 sm:grid-cols-2">{data.data.map(item => <TodoLink key={item.key} item={item} />)}</nav> :
    <EmptyState title="目前没有待办" />
}

export function TodosPage() {
  const todos = useTodos()
  return <div className="space-y-4"><h1 className="text-2xl font-semibold">我的待办</h1><Panel><Todos data={todos} /></Panel></div>
}

export function HomePage() {
  const user = useAuthStore(state => state.user)!
  const upload = navigation.some(item => item.path === '/upload' && canAccess(user.roles, item))
  const admin = user.roles.includes('admin')
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth() })
  const batches = useQuery({ queryKey: ['batches'], queryFn: getBatches, enabled: upload })
  const recent = useQuery({ queryKey: ['projects', 'home-recent'], queryFn: () => getProjects({ q: '', won: '', industry: '', company: '', page: 1, sort: 'newest' }) })
  const todos = useTodos()
  const attention = todos.data?.filter(item => item.count > 0) ?? []
  const results = batches.data ?? []
  return <div className="min-w-0 space-y-5">
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div><h1 className="text-2xl font-semibold tracking-tight">工作台首页</h1>
        <p className="mt-1 text-slate-500">你好，{user.display_name || user.username}</p></div>
      {upload && <Link to="/upload" className="ui-button ui-primary"><Icon name="upload" />上传新项目</Link>}
    </header>
    <div className="summary-layout">
      <div className="min-w-0 space-y-5">
        <Panel><div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="font-semibold">我的待办</h2><Link to="/todos" className="ui-link text-xs">查看全部待办</Link>
        </div><Todos data={todos} /></Panel>
        {upload && <section className="ui-panel overflow-hidden">
          <h2 className="px-5 py-4 font-semibold">我的上传与操作批次</h2>
          {batches.isPending ? <LoadingState>正在加载批次…</LoadingState> : batches.isError ? <ErrorState retry={() => void batches.refetch()}>{batches.error.message}</ErrorState> : results.length ?
            <Table label="我的批次"><thead><tr><th>批次</th><th>状态</th><th>最后更新</th><th>操作</th></tr></thead>
              <tbody>{results.map(batch => <tr key={batch.id}><td className="min-w-0 max-w-[32rem]">
                <p className="break-words font-medium">{batch.folder_name}</p><p className="mt-1 text-xs text-slate-500">{batch.batch_no}{batch.kind && batch.kind !== 'ingest' && ` · ${operationLabels[batch.kind]}`}</p>
              </td><td><Badge tone={batch.status.includes('异常') || batch.status.includes('失败') ? 'danger' : batch.status === '已入库' ? 'success' : 'info'}>{batch.queued ? '排队中' : batch.status}</Badge></td>
                <td className="whitespace-nowrap text-xs text-slate-500">{new Date(batch.updated_at).toLocaleString('zh-CN')}</td>
                <td><Link to={operationHref(batch.id, batch.kind, batch.status)} className="ui-link whitespace-nowrap">{['上传中', '上传中断'].includes(batch.status) ? '继续上传' : ['待核对', '已驳回'].includes(batch.status) ? '去核对' : '查看详情'}</Link></td></tr>)}</tbody></Table> :
            <EmptyState title="暂无批次"><Link to="/upload" className="ui-link">上传项目文件夹</Link></EmptyState>}
        </section>}
        <Panel><div className="mb-3 flex items-center justify-between gap-3"><h2 className="font-semibold">最近入库项目</h2><Link to="/projects" className="ui-link text-xs">浏览全部资产</Link></div>
          {recent.isPending ? <LoadingState /> : recent.isError ? <ErrorState retry={() => void recent.refetch()}>{recent.error.message}</ErrorState> : recent.data.items.length ?
            <ul className="divide-y divide-slate-100">{recent.data.items.slice(0, 5).map(item =>
              <li key={item.id}><Link to={`/projects/${item.id}`} className="flex items-center gap-3 py-2 hover:text-blue-700">
                <span className="rounded-md bg-blue-50 p-2 text-blue-600"><Icon name="folder" /></span>
                <span className="min-w-0 flex-1"><span className="block truncate font-medium">{item.project_name}</span><span className="text-xs text-slate-500">{item.project_code} · {item.company_name}</span></span>
                <Icon name="right" className="text-slate-400" />
              </Link></li>)}</ul> : <EmptyState title="暂无可访问的入库项目" />}
        </Panel>
      </div>
      <ContextPane title="工作台概览">
        <div className="divide-y divide-slate-200">
          <section className="pb-4"><h3 className="font-semibold">{user.display_name || user.username}</h3>
            <p className="mt-1 text-xs text-slate-500">{user.roles.map(role => roleLabels[role]).join(' / ')}</p></section>
          <section className="py-4"><h3 className="mb-3 font-semibold">快捷操作</h3>
            {upload && <Link to="/upload" className="ui-link mb-2 flex items-center gap-2"><Icon name="upload" />上传项目</Link>}
            <Link to="/projects" className="ui-link mb-2 flex items-center gap-2"><Icon name="folder" />浏览已入库资产</Link>
            <Link to="/search" className="ui-link flex items-center gap-2"><Icon name="search" />查找商品</Link>
            {admin && <Link to="/admin/maintenance" className="ui-link mt-2 flex items-center gap-2"><Icon name="settings" />运营管理</Link>}
          </section>
          <section className="py-4"><h3 className="mb-3 font-semibold">需要关注</h3>
            {attention.length ? <div className="space-y-2">{attention.map(item => <TodoLink key={item.key} item={item} />)}</div> :
              <p className="text-sm text-slate-500">{todos.isPending ? '正在读取…' : todos.isError ? '待办暂不可用' : '暂无待处理事项'}</p>}
          </section>
          <section className="pt-4"><h3 className="mb-3 font-semibold">系统健康</h3>
            {health.isError ? <ErrorState>{health.error.message}</ErrorState> : health.isPending ? <LoadingState /> :
              <dl className="space-y-2 text-xs text-slate-500">
                <div className="flex justify-between gap-2"><dt>服务版本</dt><dd>{health.data.version}</dd></div>
                <div className="flex justify-between gap-2"><dt>运行模式</dt><dd>{health.data.mode === 'dev' ? '开发模式' : '生产模式'}</dd></div>
                <div className="flex items-center justify-between gap-2"><dt>数据库</dt><dd><Badge tone={health.data.database_available ? 'success' : 'danger'}>{health.data.database_available ? '连接正常' : '暂不可用'}</Badge></dd></div>
              </dl>}
          </section>
        </div>
      </ContextPane>
    </div>
  </div>
}
