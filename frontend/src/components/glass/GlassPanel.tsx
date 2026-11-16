import { type PropsWithChildren } from 'react'
import { GlassSurface } from './GlassSurface'

export function GlassPanel({ children }: PropsWithChildren) {
  return <section><GlassSurface radius={32} className="p-10 shadow-xl shadow-slate-600/5">{children}</GlassSurface></section>
}
