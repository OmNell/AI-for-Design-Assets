import { request } from './client'
import type { User } from './types'

export const getMe = (signal?: AbortSignal): Promise<User> => request('/api/auth/me', { signal })
export const login = (username: string, password: string): Promise<User> => request('/api/auth/login', {
  method: 'POST', body: JSON.stringify({ username, password }),
})
export const logout = (): Promise<{ message: string }> => request('/api/auth/logout', { method: 'POST' })
