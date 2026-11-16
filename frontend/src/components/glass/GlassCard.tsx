import type { PropsWithChildren } from 'react'
import { GlassSurface } from './GlassSurface'

export function GlassCard({ children, className = '' }: PropsWithChildren<{ className?: string }>) {
  return <GlassSurface priority="secondary" radius={20} className={`p-5 ${className}`}>{children}</GlassSurface>
}
