import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { Navigate, useLocation } from 'react-router'
import { login } from '../api/auth'
import { safeLoginReturn } from '../app/navigation'
import { useAuthStore } from '../stores/auth'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { ModeSwitch } from '../components/glass/ModeSwitch'

interface FormValues { username: string; password: string }
export function LoginPage() {
  const user = useAuthStore(state => state.user)
  const notice = useAuthStore(state => state.notice)
  const [error, setError] = useState('')
  const { register, handleSubmit, resetField, formState: { isSubmitting, errors } } = useForm<FormValues>()
  const state: unknown = useLocation().state
  const returnTo = typeof state === 'object' && state !== null && 'returnTo' in state && typeof state.returnTo === 'string'
    && safeLoginReturn(state.returnTo) ? state.returnTo : '/'
  if (user) {
    const uploadRole = user.roles.some(role => ['uploader', 'first_reviewer', 'final_reviewer'].includes(role))
    return <Navigate to={returnTo === '/' && !uploadRole ? '/search' : returnTo} replace />
  }
  const submit = handleSubmit(async values => {
    setError('')
    try { useAuthStore.getState().setUser(await login(values.username, values.password)) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '登录失败，请稍后重试') }
    finally { resetField('password') }
  })
  return <div className="mx-auto min-h-screen max-w-6xl px-12 py-10">
    <header className="flex items-center justify-between"><span className="text-sm font-semibold tracking-widest">设计资产工作台</span><ModeSwitch /></header>
    <main className="grid grid-cols-2 items-center gap-20 py-28">
      <div><p className="mb-6 text-sm tracking-widest text-emerald-800">公司内网 · 设计资产</p>
        <h1 className="text-5xl leading-tight">让每一份设计，<br />有处可寻。</h1><p className="mt-7 leading-8 text-slate-500">使用你的 NAS 账号进入工作台，<br />整理、审核与查找团队的设计资产。</p></div>
      <GlassPanel><h2 className="text-2xl font-semibold">登录工作台</h2><p className="mt-2 text-sm text-slate-500">请输入你的 NAS 账号和密码</p>
        {window.location.protocol === 'http:' &&
          <p className="mt-4 rounded-lg bg-amber-100/80 p-3 text-sm text-amber-900">当前使用内网 HTTP。NAS 账号密码和登录 Cookie 会在局域网中以明文传输，请仅在公司授权网络使用。</p>}
        <form onSubmit={submit} className="mt-8 space-y-5">
          <label className="block text-sm">用户名<input className="field mt-2" autoComplete="username" {...register('username', { required: '请输入用户名' })} /></label>
          <label className="block text-sm">密码<input className="field mt-2" type="password" autoComplete="current-password" {...register('password', { required: '请输入密码' })} /></label>
          {(error || notice || errors.username || errors.password) && <p role="alert" className="text-sm text-red-800">{error || notice || errors.username?.message || errors.password?.message}</p>}
          <GlassButton type="submit" disabled={isSubmitting} className="w-full">{isSubmitting ? '正在登录…' : '登录'}</GlassButton>
        </form>
      </GlassPanel>
    </main>
  </div>
}
