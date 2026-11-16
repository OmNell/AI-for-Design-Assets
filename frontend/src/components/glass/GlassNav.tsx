import type { PropsWithChildren } from 'react'
import { GlassSurface } from './GlassSurface'

export function GlassNav({ children }: PropsWithChildren) {
  return <nav aria-label="主导航"><GlassSurface radius={24} className="px-5 py-4">{children}</GlassSurface></nav>
}
