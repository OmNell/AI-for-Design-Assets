import { NavLink } from 'react-router'

const items = [['/admin/maintenance', '运营管理'], ['/admin/roles', '角色管理'], ['/admin/tag-fields', '标签字段'], ['/admin/company-map', '对照表']]

export function AdminLinks() {
  return <nav aria-label="管理功能" className="mb-5 flex flex-wrap gap-2 border-b border-slate-200 pb-3">{items.map(([path, label]) =>
    <NavLink key={path} to={path} className={({ isActive }) =>
      `rounded-md px-3 py-2 font-medium ${isActive ? 'bg-blue-50 text-blue-700' : 'text-slate-600 hover:bg-slate-50'}`}>{label}</NavLink>)}
  </nav>
}
