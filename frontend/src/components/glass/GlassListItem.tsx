import type { PropsWithChildren } from 'react'
import { GlassSurface } from './GlassSurface'
import { useGlassMode } from './GlassProvider'

/** 长列表的条目：full 模式用实时玻璃；lite 模式只用半透明底色，不挂任何实时滤镜或背景模糊。 */
export function GlassListItem({ children, className = '', active = false }: PropsWithChildren<{ className?: string; active?: boolean }>) {
  const { mode } = useGlassMode()
  const state = active ? 'ring-2 ring-emerald-700/70' : ''
  if (mode === 'full') return <GlassSurface priority="secondary" radius={14} className={`${state} ${className}`}>{children}</GlassSurface>
  return <div data-list-item="lite" className={`rounded-[14px] border border-white/70 ${active ? 'bg-white/60' : 'bg-white/30'} ${state} ${className}`}>{children}</div>
}
