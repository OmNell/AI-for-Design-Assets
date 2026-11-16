import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate, Outlet, NavLink } from 'react-router'
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query'
import { getMe, logout } from '../api/auth'
import { getHealth } from '../api/health'
import { roleLabels } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { GlassProvider } from '../components/glass/GlassProvider'
import { GlassNav } from '../components/glass/GlassNav'
import { GlassButton } from '../components/glass/GlassButton'
import { ModeSwitch } from '../components/glass/ModeSwitch'
import { LoginPage } from '../pages/LoginPage'
import { HomePage } from '../pages/HomePage'
import { RolesPage } from '../pages/RolesPage'
import { PlaceholderPage } from '../pages/PlaceholderPage'
import { ComponentsPage } from '../pages/ComponentsPage'
import { canAccess, navigation, type NavigationItem } from './navigation'


function Guard() {
  const user = useAuthStore(state => state.user)
  return user ? <Outlet /> : <Navigate to="/login" replace />
}
function RoleGuard({ item, children }: { item: NavigationItem; children: React.ReactNode }) {
  const user = useAuthStore(state => state.user)!
  return canAccess(user.roles, item) ? children : <PlaceholderPage title="当前账号无权访问此页面" />
}
function DevComponents() {
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth() })
  return health.isPending ? <p>正在加载…</p> : health.data?.mode === 'dev' ? <ComponentsPage /> : <Navigate to="/" replace />
}
function Shell() {
  const user = useAuthStore(state => state.user)!
  const query = useQueryClient()
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth() })
  const [error, setError] = useState('')
  const [exiting, setExiting] = useState(false)
  const exit = async () => {
    setExiting(true)
    try { await logout(); query.clear(); useAuthStore.getState().clear() }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '退出失败，请稍后重试') }
    finally { setExiting(false) }
  }
  return <div className="mx-auto min-h-screen max-w-[1440px] px-10 py-7">
    <header className="mb-9"><div className="mb-5 flex items-center justify-between">
      <div className="flex items-center gap-3"><span className="brand-mark">档</span><span className="text-sm font-semibold tracking-widest">设计资产工作台</span></div>
      <div className="flex items-center gap-4"><span className="text-sm">{user.display_name || user.username}<span className="ml-3 text-xs text-slate-500">{user.roles.map(role => roleLabels[role]).join(' / ')}</span></span>
        <GlassButton onClick={exit} disabled={exiting}>退出</GlassButton><ModeSwitch /></div></div>
      <GlassNav><div className="flex flex-wrap items-center gap-x-7 gap-y-3 text-sm">{navigation.filter(item => canAccess(user.roles, item)).map(item =>
        <NavLink key={item.path} to={item.path} end={item.path === '/'} className={({ isActive }) => isActive ? 'font-semibold text-emerald-900' : 'text-slate-600 hover:text-emerald-800'}>{item.label}</NavLink>)}
        {health.data?.mode === 'dev' && <NavLink to="/components" className="text-slate-600">组件样例</NavLink>}</div></GlassNav>
    </header>{error && <p role="alert" className="mb-5 text-red-800">{error}</p>}<main><Outlet /></main>
  </div>
}
function AuthBootstrap() {
  const initialized = useAuthStore(state => state.initialized)
  const query = useQueryClient()
  const user = useAuthStore(state => state.user)
  useEffect(() => { if (!user) query.removeQueries({ queryKey: ['roles'] }) }, [user, query])
  useEffect(() => {
    const controller = new AbortController()
    getMe(controller.signal).then(current => { if (!controller.signal.aborted) useAuthStore.getState().setUser(current) })
      .catch(() => { if (!controller.signal.aborted && !useAuthStore.getState().initialized) useAuthStore.getState().clear('无法连接工作台，请重试登录') })
    return () => controller.abort()
  }, [])
  if (!initialized) return <p className="p-12 text-slate-600" role="status">正在确认登录状态…</p>
  return <Routes><Route path="/login" element={<LoginPage />} />
    <Route element={<Guard />}><Route element={<Shell />}>
      <Route index element={<HomePage />} />
      {navigation.filter(item => item.path !== '/').map(item => <Route key={item.path} path={item.path} element={<RoleGuard item={item}>
        {item.path === '/admin/roles' ? <RolesPage /> : <PlaceholderPage title={item.label} />}
      </RoleGuard>} />)}
      <Route path="/components" element={<DevComponents />} /><Route path="*" element={<PlaceholderPage title="页面不存在" />} />
    </Route></Route></Routes>
}
export function App() {
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } }))
  return <QueryClientProvider client={queryClient}><GlassProvider><BrowserRouter><AuthBootstrap /></BrowserRouter></GlassProvider></QueryClientProvider>
}
