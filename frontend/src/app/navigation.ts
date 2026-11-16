import type { Role } from '../api/types'

export interface NavigationItem { path: string; label: string; roles?: Role[] }
export const navigation: NavigationItem[] = [
  { path: '/', label: '首页' },
  { path: '/upload', label: '上传', roles: ['uploader', 'first_reviewer', 'final_reviewer'] },
  { path: '/first-review', label: '待初审', roles: ['first_reviewer'] },
  { path: '/final-review', label: '待终审', roles: ['final_reviewer'] },
  { path: '/projects', label: '项目' }, { path: '/search', label: '搜索' },
  { path: '/admin/roles', label: '管理', roles: ['admin'] }, { path: '/settings', label: '设置', roles: ['admin'] },
]
export function canAccess(roles: Role[], item: NavigationItem): boolean {
  return !item.roles || roles.some(role => item.roles!.includes(role))
}
