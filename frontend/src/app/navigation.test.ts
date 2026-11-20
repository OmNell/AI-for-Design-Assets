import { expect, it } from 'vitest'
import { canAccess, navigation } from './navigation'
import type { Role } from '../api/types'
const visible = (roles: Role[]) => navigation.filter(item => canAccess(roles, item)).map(item => item.label)
it('上传人没有审核和管理菜单', () => {
  expect(visible(['uploader'])).toEqual(['工作台首页', '资产入库', '我的待办', '全部资产', '智能检索'])
})
it('审核人继承上传能力，但不互相继承审核权限', () => {
  expect(visible(['first_reviewer'])).toEqual(['工作台首页', '资产入库', '待初审', '我的待办', '全部资产', '智能检索'])
  expect(visible(['final_reviewer'])).toContain('待终审')
  expect(visible(['final_reviewer'])).not.toContain('待初审')
})
it('管理员不能自动审核，多角色组合取并集', () => {
  expect(visible(['admin'])).toEqual(['工作台首页', '我的待办', '全部资产', '智能检索', '管理', '设置'])
  expect(visible(['admin', 'final_reviewer'])).toContain('待终审')
})
