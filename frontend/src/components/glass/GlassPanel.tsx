import LiquidGlass from 'liquid-glass-react'
import { type PropsWithChildren } from 'react'
import { useGlass } from './GlassProvider'

export function GlassPanel({ children }: PropsWithChildren) {
  const { mode } = useGlass()
  // 上游声明要求 React 19；保守保留 React 18，以半透明容器承载首页。
  // 安装该库并集中封装入口，待兼容性确认后启用实时滤镜。
  void LiquidGlass
  return <section data-visual-mode={mode} className="glass-panel rounded-[32px] border border-white/70 bg-white/55 p-10 shadow-xl shadow-slate-600/5 backdrop-blur-xl">{children}</section>
}
