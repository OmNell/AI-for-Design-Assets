import { lazy, Suspense, useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate, Outlet, useLocation } from 'react-router'
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query'
import { getMe } from '../api/auth'
import { getHealth } from '../api/health'
import { useAuthStore } from '../stores/auth'
import { Shell } from './Shell'
import { LoginPage } from '../pages/LoginPage'
import { HomePage, TodosPage } from '../pages/HomePage'
import { UploadPage } from '../pages/UploadPage'
import { RolesPage } from '../pages/RolesPage'
import { PlaceholderPage } from '../pages/PlaceholderPage'
import { ComponentsPage } from '../pages/ComponentsPage'
import { AiSettingsPage } from '../pages/AiSettingsPage'
import { TagFieldsPage } from '../pages/TagFieldsPage'
import { BatchDetailPage } from '../pages/BatchDetailPage'
import { PreDescriptionPage } from '../pages/PreDescriptionPage'
import { ReviewPage } from '../pages/ReviewPage'
import { AuditPage } from '../pages/AuditPage'
import { QueuePage } from '../pages/QueuePage'
import { CompanyMapPage } from '../pages/CompanyMapPage'
import { SearchPage } from '../pages/SearchPage'
import { AssetsPage } from '../pages/AssetsPage'
import { ProjectPage } from '../pages/ProjectPage'
import { ProductPage } from '../pages/ProductPage'
import { AssetLinkPage, FilePage, FindingsPage } from '../pages/AssetLinkPage'
import { ProjectOperationsPage, VoidedProjectsPage } from '../pages/ProjectOperationsPage'
import { OperationPage } from '../pages/OperationPage'
import { safeLoginReturn, canAccess, navigation, type NavigationItem } from './navigation'


const NasSettingsPage = lazy(() => import('../pages/NasSettingsPage').then(module => ({ default: module.NasSettingsPage })))
const MaintenancePage = lazy(() => import('../pages/MaintenancePage').then(module => ({ default: module.MaintenancePage })))
const ArchiveRepairsPage = lazy(() => import('../pages/MaintenancePage').then(module => ({ default: module.ArchiveRepairsPage })))


function Guard() {
  const user = useAuthStore(state => state.user)
  const location = useLocation()
  const returnTo = safeLoginReturn(location.pathname) ? location.pathname : undefined
  return user ? <Outlet /> : <Navigate to="/login" state={{ returnTo }} replace />
}
function RoleGuard({ item, children }: { item: NavigationItem; children: React.ReactNode }) {
  const user = useAuthStore(state => state.user)!
  return canAccess(user.roles, item) ? children : <PlaceholderPage title="当前账号无权访问此页面" />
}
function DevComponents() {
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth() })
  return health.isPending ? <p>正在加载…</p> : health.data?.mode === 'dev' ? <ComponentsPage /> : <Navigate to="/" replace />
}
function AuthBootstrap() {
  const initialized = useAuthStore(state => state.initialized)
  const query = useQueryClient()
  const user = useAuthStore(state => state.user)
  useEffect(() => { if (!user) query.clear() }, [user, query])
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
      <Route path="/todos" element={<TodosPage />} />
      {navigation.filter(item => item.path !== '/' && item.path !== '/todos').map(item => <Route key={item.path} path={item.path} element={<RoleGuard item={item}>
        {item.path === '/search' ? <SearchPage /> : item.path === '/projects' ? <AssetsPage /> : item.path === '/admin/roles' ? <RolesPage /> : item.path === '/upload' ? <UploadPage /> : item.path === '/admin/nas-settings' ? <Suspense fallback={<p role="status">正在加载 NAS 存储设置…</p>}><NasSettingsPage /></Suspense> : item.path === '/settings' ? <AiSettingsPage /> : item.path === '/first-review' ? <QueuePage stage="first" /> : item.path === '/final-review' ? <QueuePage stage="final" /> : <PlaceholderPage title={item.label} />}
      </RoleGuard>} />)}
      <Route path="/admin/maintenance" element={<RoleGuard item={navigation.find(item => item.path === '/admin/roles')!}><Suspense fallback={<p role="status">正在读取维护页面…</p>}><MaintenancePage /></Suspense></RoleGuard>} />
      <Route path="/archive/repairs" element={<RoleGuard item={navigation.find(item => item.path === '/final-review')!}><Suspense fallback={<p role="status">正在读取修复申请…</p>}><ArchiveRepairsPage /></Suspense></RoleGuard>} />
      <Route path="/admin/tag-fields" element={<RoleGuard item={navigation.find(item => item.path === '/admin/roles')!}><TagFieldsPage /></RoleGuard>} />
      <Route path="/projects/:id" element={<ProjectPage />} />
      <Route path="/projects/voided" element={<VoidedProjectsPage />} />
      <Route path="/projects/:id/operations" element={<ProjectOperationsPage />} />
      <Route path="/batches/:id/operation" element={<OperationPage />} />
      <Route path="/products/:id" element={<ProductPage />} />
      <Route path="/files/:id" element={<FilePage />} />
      <Route path="/l/:linkId" element={<AssetLinkPage />} />
      <Route path="/admin/asset-findings" element={<RoleGuard item={navigation.find(item => item.path === '/admin/roles')!}><FindingsPage /></RoleGuard>} />
      <Route path="/batches/:id" element={<BatchDetailPage />} />
      <Route path="/batches/:id/pre-descriptions" element={<PreDescriptionPage />} />
      <Route path="/batches/:id/review" element={<ReviewPage />} />
      <Route path="/review/:id" element={<AuditPage />} />
      <Route path="/admin/company-map" element={<RoleGuard item={navigation.find(item => item.path === '/admin/roles')!}><CompanyMapPage /></RoleGuard>} />
      <Route path="/components" element={<DevComponents />} /><Route path="*" element={<PlaceholderPage title="页面不存在" />} />
    </Route></Route></Routes>
}
export function App() {
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } }))
  return <QueryClientProvider client={queryClient}><BrowserRouter><AuthBootstrap /></BrowserRouter></QueryClientProvider>
}
