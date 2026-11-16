import LiquidGlass from 'liquid-glass-react'
import { useLayoutEffect, useRef, useState, type PropsWithChildren } from 'react'
import { useGlass } from './GlassProvider'

interface GlassSurfaceProps extends PropsWithChildren {
  as?: 'div' | 'span'
  className?: string
  radius?: number
  priority?: 'primary' | 'secondary'
}

function supportsRefraction(): boolean {
  const agent = navigator.userAgent
  // Safari/Firefox 的 SVG 背景位移支持不完整，保留可用的毛玻璃。
  const partial = /Firefox|Safari/.test(agent) && !/Chrome|Chromium|Edg/.test(agent)
  return !partial && typeof CSS !== 'undefined' && CSS.supports('backdrop-filter', 'blur(1px)')
}

export function GlassSurface({ children, className = '', radius = 24, priority = 'primary', as: Element = 'div' }: GlassSurfaceProps) {
  const { mode } = useGlass()
  const container = useRef<HTMLElement | null>(null)
  const [sizeKey, setSizeKey] = useState('initial')
  useLayoutEffect(() => {
    const element = container.current
    if (!element) return
    const update = () => {
      const box = element.getBoundingClientRect()
      setSizeKey(`${Math.round(box.width)}x${Math.round(box.height)}`)
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const active = mode === 'full' || priority === 'primary'
  const renderer = active ? supportsRefraction() ? 'svg' : 'css' : 'none'

  const Content = Element
  return <Element ref={node => { container.current = node }} className={`glass-surface ${active ? '' : 'bg-white/25'} ${className}`} style={{ borderRadius: radius }}
    data-visual-mode={mode} data-glass-renderer={renderer}>
    {renderer === 'svg' && <div className="glass-effect" aria-hidden="true">
      <LiquidGlass key={sizeKey} className="glass-library" mode="standard" cornerRadius={radius}
        displacementScale={60} blurAmount={0.0625} saturation={125} aberrationIntensity={2}
        elasticity={0} padding="0" mouseContainer={container}
        style={{ position: 'absolute', inset: '0', top: '0px', left: '0px', width: '100%', height: '100%' }}>
        <span />
      </LiquidGlass>
    </div>}
    <Content className="glass-content">{children}</Content>
  </Element>
}
