import { request } from './client'
import type { Role, User } from './types'

export const listUsers = (): Promise<User[]> => request('/api/admin/roles')
export const updateUser = (user_id: number, roles: Role[], enabled: boolean): Promise<User> =>
  request('/api/admin/roles', { method: 'PUT', body: JSON.stringify({ user_id, roles, enabled }) })
