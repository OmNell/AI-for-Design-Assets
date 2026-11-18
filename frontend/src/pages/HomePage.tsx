import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { getBatches } from '../api/upload'
import { getHealth } from '../api/health'
import { getTodos, type Todo } from '../api/audit'
import { useAuthStore } from '../stores/auth'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassCard } from '../components/glass/GlassCard'
import { canAccess, navigation } from '../app/navigation'

function TodoLink({ item }: { item: Todo }) {
  const count = <span className="rounded-full bg-emerald-800 px-2 py-0.5 text-xs font-semibold text-white">{item.count}</span>
  const style = "flex items-center gap-2 rounded-full border border-white/70 bg-white/50 px-4 py-2 text-sm hover:bg-white/75"
  if (item.link !== '/') return <Link to={item.link} className={style}>{item.label}{count}</Link>
  return <details><summary className={style}>{item.label}{count}</summary>
    <ul className="mt-2 space-y-1">{item.batches.map(batch => {
      const path = (item.key === 'overdue' || item.key === 'ingest') ? `/review/${batch.id}` : item.key === 'ai_failed' ? `/batches/${batch.id}` : `/batches/${batch.id}/review`
      return <li key={batch.id}><Link to={path} className="text-sm text-emerald-800 underline">{batch.label}</Link></li>
    })}</ul></details>
}

export function HomePage() {
  const user = useAuthStore(state => state.user)!
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth(), retry: false })
  const upload = canAccess(user.roles, navigation.find(item => item.path === '/upload')!)
  const batches = useQuery({ queryKey: ['batches'], queryFn: getBatches, retry: false, enabled: upload })
  const todos = useQuery({ queryKey: ['todos'], queryFn: getTodos, retry: false, refetchInterval: 30000 })
  return <div className="space-y-7"><div><p className="mb-4 text-xs tracking-widest text-emerald-800">公司内网 · 设计资产</p>
    <h1 className="text-4xl font-semibold">你好，{user.display_name || user.username}</h1><p className="mt-4 text-slate-500">从这里开始整理与查找团队的设计资产。</p></div>
    {!!todos.data?.length && <nav aria-label="待办" data-testid="todos" className="flex flex-wrap gap-3">{todos.data.map(item => <TodoLink key={item.key} item={item} />)}</nav>}
    <GlassPanel><h2 className="text-xl font-semibold">我的工作台</h2><div className="mt-5 space-y-4">{batches.data?.length ? batches.data.map(batch => <div key={batch.id} className="flex items-center justify-between border-b border-slate-400/15 py-3 text-sm"><div><p>{batch.batch_no} · {batch.folder_name}</p><p className="mt-2 text-xs text-slate-500">{batch.queued ? '排队中' : batch.status} · {new Date(batch.updated_at).toLocaleString('zh-CN')}</p></div><Link to={['上传中', '上传中断'].includes(batch.status) ? `/upload?resume=${batch.id}` : ['待核对', '已驳回'].includes(batch.status) ? `/batches/${batch.id}/review` : `/batches/${batch.id}`} className="text-emerald-800 underline underline-offset-4">{['上传中', '上传中断'].includes(batch.status) ? '继续上传' : ['待核对', '已驳回'].includes(batch.status) ? '去核对' : '查看详情'}</Link></div>) : <p className="text-slate-500">{!upload ? '当前账号没有上传角色。' : batches.isPending ? '正在加载批次…' : batches.isError ? '批次加载失败，请刷新重试。' : '暂无批次，请上传一个项目文件夹。'}</p>}</div>
      <div className="mt-6 flex gap-6 text-sm text-emerald-800">{upload && <Link to="/upload" className="underline underline-offset-4">上传新项目</Link>}<Link to="/projects" className="underline underline-offset-4">对已入库项目操作</Link></div>
    </GlassPanel>
    <div className="grid grid-cols-3 gap-5"><GlassCard><p className="text-sm text-slate-500">服务版本</p><p className="mt-4 text-2xl">{health.data ? `v${health.data.version}` : '—'}</p></GlassCard>
      <GlassCard><p className="text-sm text-slate-500">运行模式</p><p className="mt-4 text-2xl">{health.data ? health.data.mode === 'dev' ? '开发模式' : '生产模式' : '—'}</p></GlassCard>
      <GlassCard><p className="text-sm text-slate-500">数据库</p><p className="mt-4 text-2xl">{health.data?.database_available ? '连接正常' : '正在检查'}</p></GlassCard></div>
    {health.isError && <p role="alert" className="text-red-800">{health.error.message}</p>}
  </div>
}
