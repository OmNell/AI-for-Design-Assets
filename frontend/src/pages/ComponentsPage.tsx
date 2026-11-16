import { useState } from 'react'
import { GlassButton } from '../components/glass/GlassButton'
import { GlassCard } from '../components/glass/GlassCard'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassModal } from '../components/glass/GlassModal'
import { GlassNav } from '../components/glass/GlassNav'
import { useGlassMode } from '../components/glass/GlassProvider'

export function ComponentsPage() {
  const [open, setOpen] = useState(false)
  const { mode } = useGlassMode()
  return <div className="space-y-7"><GlassPanel><h1 className="text-2xl font-semibold">组件样例</h1><p className="mt-3 text-slate-500">当前：{mode === 'full' ? '完整模式' : '轻量模式'}。用右上角切换并滚动下方 60 张卡片比较流畅度。</p>
    <div className="mt-6 flex gap-4"><GlassButton onClick={() => setOpen(true)}>打开弹窗</GlassButton><GlassButton primary={false}>次要按钮</GlassButton><GlassButton disabled>禁用按钮</GlassButton></div>
    <div className="mt-6"><GlassNav>GlassNav · 导航容器</GlassNav></div></GlassPanel>
    <div className="grid grid-cols-4 gap-5">{Array.from({ length: 60 }, (_, index) => <GlassCard key={index}>
      <p className="text-xs text-emerald-800">GlassCard · {String(index + 1).padStart(2, '0')}</p><h2 className="mt-4 text-lg font-semibold">设计资产卡片</h2><p className="mt-3 text-sm text-slate-500">轻量模式关闭条目实时滤镜，保留半透明与模糊。</p>
    </GlassCard>)}</div>
    <GlassModal open={open} title="GlassModal · 弹窗" onClose={() => setOpen(false)}><p className="text-slate-500">按 Escape 或关闭按钮返回。两种模式下均保留玻璃容器。</p></GlassModal>
  </div>
}
