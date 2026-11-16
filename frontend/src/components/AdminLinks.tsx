import { NavLink } from 'react-router'

export function AdminLinks() {
  return <nav aria-label="管理功能" className="mb-6 flex gap-6 text-sm">
    {[['/admin/roles', '角色管理'], ['/admin/tag-fields', '标签字段']].map(([path, label]) => <NavLink key={path} to={path} className={({ isActive }) => isActive ? 'font-semibold text-emerald-900' : 'text-slate-500 underline underline-offset-4'}>{label}</NavLink>)}
  </nav>
}
