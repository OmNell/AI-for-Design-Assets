import { useEffect, useState } from 'react'
import { getHealth, type Health } from '../api/health'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassSurface } from '../components/glass/GlassSurface'
import { useGlass } from '../components/glass/GlassProvider'

export function HomePage() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')
  const { mode, toggle } = useGlass()
  useEffect(() => {
    const controller = new AbortController()
    getHealth(controller.signal).then(setHealth).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '服务暂时不可用。')
    })
    return () => controller.abort()
  }, [])
  return <div className="mx-auto min-h-screen max-w-6xl px-12 py-10">
    <header><GlassSurface className="px-5 py-4"><div className="flex items-center justify-between">
      <div className="flex items-center gap-3"><span className="brand-mark">档</span><span className="text-sm font-semibold tracking-widest">设计资产工作台</span></div>
      <button type="button" onClick={toggle} className="rounded-full text-sm text-slate-600 hover:text-emerald-900 focus-visible:outline-2 focus-visible:outline-emerald-700" aria-label="切换视觉模式">
        <GlassSurface radius={999} className="px-5 py-2">视觉模式 · {mode === 'full' ? '完整' : '轻量'}</GlassSurface>
      </button>
    </div></GlassSurface></header>
    <main className="py-20">
      <p className="mb-5 text-xs tracking-[0.3em] text-emerald-800">公司内网 · 设计资产</p>
      <h1 className="text-5xl font-medium tracking-tight text-slate-800">让每一份设计，有处可寻。</h1>
      <p className="mb-12 mt-5 text-base leading-8 text-slate-500">设计资产工作台正在搭建中，先确认服务已准备就绪。</p>
      <GlassPanel>
        <div className="flex items-start justify-between"><div><h2 className="text-xl font-semibold">工作台运行状态</h2><p className="mt-2 text-sm text-slate-500">当前为基础骨架，业务功能将在后续开放。</p></div>
          <span role="status" className="rounded-full bg-emerald-900/5 px-4 py-2 text-sm text-emerald-800">{error ? '连接异常' : health ? '已连接' : '正在连接…'}</span></div>
        {error ? <p role="alert" className="mt-8 text-red-800">{error}</p> :
          <div className="mt-10 grid grid-cols-3 gap-8 border-t border-slate-500/10 pt-7">
            <div><GlassSurface priority="secondary" radius={16} className="p-4"><dl><dt className="text-xs text-slate-500">服务版本</dt><dd className="mt-3 text-2xl">{health ? `v${health.version}` : '—'}</dd></dl></GlassSurface></div>
            <div><GlassSurface priority="secondary" radius={16} className="p-4"><dl><dt className="text-xs text-slate-500">运行模式</dt><dd className="mt-3 text-2xl">{health ? health.mode === 'dev' ? '开发模式' : '生产模式' : '—'}</dd></dl></GlassSurface></div>
            <div><GlassSurface priority="secondary" radius={16} className="p-4"><dl><dt className="text-xs text-slate-500">数据库</dt><dd className="mt-3 text-2xl">{health ? health.database_available ? '连接正常' : '连接异常' : '—'}</dd></dl></GlassSurface></div>
          </div>}
      </GlassPanel>
      <p className="mt-6 text-xs text-slate-400">项目骨架 · WP01</p>
    </main>
    <footer className="border-t border-slate-700/10 pt-5 text-xs text-slate-400">设计资产工作台<span className="float-right">内部使用</span></footer>
  </div>
}
