import { afterEach, expect, it, vi } from 'vitest'
import { request } from './client'
import { useAuthStore } from '../stores/auth'
const user = { id: 1, username: 'demo', display_name: null, roles: [] , enabled: true }
afterEach(() => { vi.unstubAllGlobals(); useAuthStore.getState().clear() })
it('NAS 超时 401 清除用户并保留准确提示', async () => {
  useAuthStore.getState().setUser(user)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'nas_session_expired', message: '其他消息' }), { status: 401 })))
  await expect(request('/api/auth/me')).rejects.toThrow('NAS 连接已超时，请重新登录')
  expect(useAuthStore.getState().user).toBeNull()
  expect(useAuthStore.getState().notice).toBe('NAS 连接已超时，请重新登录')
})
it('403 不误清登录状态', async () => {
  useAuthStore.getState().setUser(user)
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"code":"forbidden","message":"无权限"}', { status: 403 })))
  await expect(request('/api/admin/roles')).rejects.toThrow('无权限')
  expect(useAuthStore.getState().user).toEqual(user)
})
it('网络错误提供中文提示，不泄露上游错误', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('原始异常')))
  await expect(request('/api/auth/me')).rejects.toThrow('无法连接工作台')
})
