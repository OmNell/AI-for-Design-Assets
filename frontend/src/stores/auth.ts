import { create } from 'zustand'
import type { User } from '../api/types'

interface AuthState {
  user: User | null
  initialized: boolean
  notice: string
  setUser: (user: User | null) => void
  clear: (notice?: string) => void
}
// 令牌只在 HttpOnly Cookie；账号状态不写 localStorage。
export const useAuthStore = create<AuthState>(set => ({
  user: null, initialized: false, notice: '',
  setUser: user => set({ user, initialized: true, notice: '' }),
  clear: (notice = '') => set({ user: null, initialized: true, notice }),
}))
