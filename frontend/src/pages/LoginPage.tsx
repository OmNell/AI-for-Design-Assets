import { Button, Icon, Panel } from '../components/ui'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { Navigate, useLocation } from 'react-router'
import { login } from '../api/auth'
import { getHealth } from '../api/health'
import { safeLoginReturn } from '../app/navigation'
import { useAuthStore } from '../stores/auth'
import { DsmLocalTestNotice } from '../components/DsmLocalTestNotice'

interface FormValues { username: string; password: string }
export function LoginPage() {
  const user = useAuthStore(state => state.user)
  const notice = useAuthStore(state => state.notice)
  const health = useQuery({ queryKey: ['health'], queryFn: () => getHealth() })
  const [error, setError] = useState('')
  const { register, handleSubmit, resetField, formState: { isSubmitting, errors } } = useForm<FormValues>()
  const state: unknown = useLocation().state
  const returnTo = typeof state === 'object' && state !== null && 'returnTo' in state && typeof state.returnTo === 'string'
    && safeLoginReturn(state.returnTo) ? state.returnTo : '/'
  if (user) return <Navigate to={returnTo} replace />
  const submit = handleSubmit(async values => {
    setError('')
    try { useAuthStore.getState().setUser(await login(values.username, values.password)) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '登录失败，请稍后重试') }
    finally { resetField('password') }
  })
  return <main className="grid min-h-dvh place-items-center bg-slate-50 p-6">
    <div className="w-full max-w-sm">
      <div className="mb-6 flex items-center gap-3"><span className="brand-mark"><Icon name="brand" className="size-5" /></span>
        <span className="font-semibold">设计资产工作台</span></div>
      <Panel><h1 className="text-2xl font-semibold tracking-tight">登录工作台</h1><p className="mt-2 text-slate-500">使用你的 NAS 账号与密码登录。</p>
        {health.data?.dsm_verify_ssl === false && <div className="mt-4 overflow-hidden rounded-lg"><DsmLocalTestNotice /></div>}
        {window.location.protocol === 'http:' &&
          <p className="mt-4 rounded-lg bg-amber-100/80 p-3 text-sm text-amber-900">当前使用内网 HTTP。NAS 账号密码和登录 Cookie 会在局域网中以明文传输，请仅在公司授权网络使用。</p>}
        <form onSubmit={submit} className="mt-8 space-y-5">
          <label className="block text-sm">用户名<input className="field mt-2" autoComplete="username" {...register('username', { required: '请输入用户名' })} /></label>
          <label className="block text-sm">密码<input className="field mt-2" type="password" autoComplete="current-password" {...register('password', { required: '请输入密码' })} /></label>
          {(error || notice || errors.username || errors.password) && <p role="alert" className="text-sm text-red-800">{error || notice || errors.username?.message || errors.password?.message}</p>}
          <Button type="submit" disabled={isSubmitting} className="w-full">{isSubmitting ? '正在登录…' : '登录'}</Button>
        </form>
      </Panel>
    </div>
  </main>
}
