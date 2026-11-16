import type { PropsWithChildren } from 'react'
import { useGlassMode } from './GlassProvider'

/**
 * 长列表的条目。条目位于可滚动容器内，折射滤镜在滚动裁剪区里取不到背后的画面，会渲染成灰块，
 * 所以两种模式都不挂 SVG 折射：full 模式用半透明加背景模糊，lite 模式只有半透明底色（不做任何实时滤镜）。
 */
export function GlassListItem({ children, className = '', active = false }: PropsWithChildren<{ className?: string; active?: boolean }>) {
  const { mode } = useGlassMode()
  const state = active ? 'ring-2 ring-emerald-700/70' : ''
  return <div data-list-item={mode} className={`rounded-[14px] border border-white/70 ${active ? 'bg-white/60' : 'bg-white/30'} ${mode === 'full' ? 'backdrop-blur-md backdrop-saturate-150' : ''} ${state} ${className}`}>{children}</div>
}
