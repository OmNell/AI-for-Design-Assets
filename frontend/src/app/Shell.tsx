import { useLayoutEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { logout } from '../api/auth'
import { getHealth } from '../api/health'
import { roleLabels } from '../api/types'
import { DsmLocalTestNotice } from '../components/DsmLocalTestNotice'
import { Button, ErrorState, Icon, Input, type IconName } from '../components/ui'
import { useAuthStore } from '../stores/auth'
import { canAccess, navigation } from './navigation'

const icons: Record<string, IconName> = {
  '/': 'home', '/projects': 'folder', '/upload': 'upload', '/todos': 'inbox', '/search': 'search',
  '/first-review': 'check', '/final-review': 'check', '/admin/roles': 'users', '/settings': 'settings', '/admin/nas-settings': 'folder',
}

export function Shell() {
  const user = useAuthStore(state => state.user)!
  const query = useQueryClient()
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth() })
  const macDsmTest = health.data?.dsm_verify_ssl === false
  const [error, setError] = useState('')
  const [exiting, setExiting] = useState(false)
  const [term, setTerm] = useState('')
  const navigate = useNavigate()
  const location = useLocation()
  const workspace = useRef<HTMLElement>(null)
  useLayoutEffect(() => {
    if (workspace.current) {
      workspace.current.scrollTop = 0
      workspace.current.scrollLeft = 0
    }
  }, [location.pathname])
  const exit = async () => {
    setExiting(true)
    try { await logout(); query.clear(); useAuthStore.getState().clear() }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '退出失败，请稍后重试') }
    finally { setExiting(false) }
  }
  const name = user.display_name || user.username
  const sections = [
    { label: '工作区', paths: ['/', '/projects', '/upload', '/todos', '/search'] },
    { label: '审核', paths: ['/first-review', '/final-review'] },
    { label: '系统', paths: ['/admin/roles', '/settings', '/admin/nas-settings'] },
  ]
  return <div className="app-shell" data-notice={macDsmTest}>
    <header className="app-header col-span-2 border-b border-slate-200 bg-white">
      <Link to="/" aria-label="设计资产工作台" className="app-brand flex shrink-0 items-center gap-3">
        <span className="brand-mark"><Icon name="brand" className="size-5" /></span><span className="app-brand-label font-semibold text-slate-900">设计资产工作台</span>
      </Link>
      <form className="relative min-w-0 max-w-[360px] flex-1" onSubmit={event => {
        event.preventDefault()
        if (term.trim()) navigate(`/search?q=${encodeURIComponent(term.trim())}`)
      }}>
        <Icon name="search" className="pointer-events-none absolute left-3 top-2.5 text-slate-400" />
        <Input maxLength={250} aria-label="全局搜索" placeholder="搜索项目、商品、编号…" className="!bg-slate-50 !pl-9" value={term} onChange={event => setTerm(event.target.value)} />
      </form>
      <div className="app-account ml-auto flex min-w-0 shrink items-center">
        <span className="grid size-8 shrink-0 place-items-center rounded-full bg-blue-100 font-semibold text-blue-700">{Array.from(name)[0]?.toUpperCase()}</span>
        <div className="app-account-details min-w-0">
          <p className="max-w-40 truncate font-medium" title={name}>{name}</p>
          <p className="max-w-40 truncate text-xs text-slate-500">{user.roles.map(role => roleLabels[role]).join(' / ')}</p>
        </div>
        <Button className="app-logout" primary={false} aria-label="退出" loading={exiting} onClick={() => void exit()}><Icon name="logout" /><span>退出</span></Button>
      </div>
    </header>
    {macDsmTest && <div className="col-span-2"><DsmLocalTestNotice /></div>}
    <aside className="app-sidebar flex min-h-0 flex-col overflow-auto border-r border-slate-200 bg-white pt-5">
      <nav className="space-y-6" aria-label="主导航">{sections.map(section => {
        const links = section.paths.flatMap(path => navigation.filter(item => item.path === path && canAccess(user.roles, item)))
        return links.length > 0 && <div key={section.label}>
          <p className="app-nav-label mb-2 px-3 text-xs font-medium text-slate-500">{section.label}</p>
          <ul className="space-y-1">{links.map(item => <li key={item.path}>
            <NavLink to={item.path} end={item.path === '/'} aria-label={item.label} title={item.label} className={({ isActive }) => {
              const active = isActive || (item.path === '/admin/roles' && location.pathname.startsWith('/admin/') && location.pathname !== '/admin/nas-settings')
              return `app-nav-link rounded-lg py-2.5 font-medium ${active ? 'bg-blue-50 text-blue-700' : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'}`
            }}><Icon name={icons[item.path]} /><span>{item.label}</span></NavLink>
          </li>)}</ul>
        </div>
      })}{health.data?.mode === 'dev' && <NavLink to="/components" aria-label="组件样例" title="组件样例" className="app-nav-link rounded-lg py-2.5 text-slate-600 hover:bg-slate-50"><Icon name="grid" /><span>组件样例</span></NavLink>}</nav>
    </aside>
    <main id="workspace" ref={workspace} className="workspace min-h-0 min-w-0 overflow-auto">
      {error && <div className="mb-4"><ErrorState>{error}</ErrorState></div>}<Outlet />
    </main>
  </div>
}
