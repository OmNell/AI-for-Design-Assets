import type { ButtonHTMLAttributes } from 'react'
import { GlassSurface } from './GlassSurface'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> { primary?: boolean }
export function GlassButton({ children, className = '', primary = true, type = 'button', ...props }: Props) {
  return <button {...props} type={type} className={`glass-button rounded-full text-sm font-medium focus-visible:outline-2 focus-visible:outline-emerald-700 disabled:cursor-not-allowed disabled:opacity-50 ${className}`}>
    <GlassSurface as="span" radius={999} priority={primary ? 'primary' : 'secondary'} className="block px-5 py-2.5">{children}</GlassSurface>
  </button>
}
