import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { getHealth } from '../api/health'
import { useAuthStore } from '../stores/auth'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassCard } from '../components/glass/GlassCard'
import { canAccess, navigation } from '../app/navigation'

export function HomePage() {
  const user = useAuthStore(state => state.user)!
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth(), retry: false })
  const upload = canAccess(user.roles, navigation.find(item => item.path === '/upload')!)
  return <div className="space-y-7"><div><p className="mb-4 text-xs tracking-widest text-emerald-800">公司内网 · 设计资产</p>
    <h1 className="text-4xl font-semibold">你好，{user.display_name || user.username}</h1><p className="mt-4 text-slate-500">从这里开始整理与查找团队的设计资产。</p></div>
    <GlassPanel><h2 className="text-xl font-semibold">我的工作台</h2><p className="mt-4 text-slate-500">批次与待办功能建设中，当前尚无可展示的数据。</p>
      <div className="mt-6 flex gap-6 text-sm text-emerald-800">{upload && <Link to="/upload" className="underline underline-offset-4">上传新项目</Link>}<Link to="/projects" className="underline underline-offset-4">对已入库项目操作</Link></div>
    </GlassPanel>
    <div className="grid grid-cols-3 gap-5"><GlassCard><p className="text-sm text-slate-500">服务版本</p><p className="mt-4 text-2xl">{health.data ? `v${health.data.version}` : '—'}</p></GlassCard>
      <GlassCard><p className="text-sm text-slate-500">运行模式</p><p className="mt-4 text-2xl">{health.data ? health.data.mode === 'dev' ? '开发模式' : '生产模式' : '—'}</p></GlassCard>
      <GlassCard><p className="text-sm text-slate-500">数据库</p><p className="mt-4 text-2xl">{health.data?.database_available ? '连接正常' : '正在检查'}</p></GlassCard></div>
    {health.isError && <p role="alert" className="text-red-800">{health.error.message}</p>}
  </div>
}
