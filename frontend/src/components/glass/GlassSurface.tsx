import LiquidGlass from 'liquid-glass-react'
import { useEffect, useLayoutEffect, useRef, useState, type PropsWithChildren } from 'react'
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

/** 位移贴图相对元素尺寸的放大倍数。 */
export function mapScale(devicePixelRatio: number): number {
  return Math.max(1, Math.ceil(devicePixelRatio - 1e-6))
}

/** 屏幕缩放（高分屏、系统 125%/150%、浏览器缩放）变化时返回最新的设备像素比。 */
function useDevicePixelRatio(): number {
  const [ratio, setRatio] = useState(() => window.devicePixelRatio || 1)
  useEffect(() => {
    const update = () => setRatio(window.devicePixelRatio || 1)
    window.addEventListener('resize', update)
    const media = typeof window.matchMedia === 'function' ? window.matchMedia(`(resolution: ${ratio}dppx)`) : null
    media?.addEventListener?.('change', update)
    return () => { window.removeEventListener('resize', update); media?.removeEventListener?.('change', update) }
  }, [ratio])
  return ratio
}

export function GlassSurface({ children, className = '', radius = 24, priority = 'primary', as: Element = 'div' }: GlassSurfaceProps) {
  const { mode } = useGlass()
  const container = useRef<HTMLElement | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const ratio = useDevicePixelRatio()
  useLayoutEffect(() => {
    const element = container.current
    if (!element) return
    const update = () => {
      const box = element.getBoundingClientRect()
      const next = { width: Math.round(box.width), height: Math.round(box.height) }
      setSize(current => current.width === next.width && current.height === next.height ? current : next)
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const active = mode === 'full' || priority === 'primary'
  const renderer = active ? supportsRefraction() ? 'svg' : 'css' : 'none'
  const sizeKey = `${size.width}x${size.height}`
  useLayoutEffect(() => {
    // Chromium 在屏幕缩放不是 100% 时，feDisplacementMap 读取位移贴图不随缩放放大：库自带的贴图只盖住
    // 左上角一部分，其余区域被整体平移，表现为右侧和底部的深色条带、四边没有折射（按 README 原样使用也一样）。
    // 实测 0.8～3 倍缩放：贴图按「缩放向上取整」的倍数放大即可贴合四条边；偏大只会让边缘折射略弱，不会出现条带。
    if (renderer !== 'svg' || !size.width || !size.height) return
    const factor = mapScale(ratio)
    container.current?.querySelectorAll(':scope > .glass-effect feImage').forEach(image => {
      image.setAttribute('width', String(size.width * factor))
      image.setAttribute('height', String(size.height * factor))
    })
  }, [renderer, size, ratio])

  const Content = Element
  return <Element ref={node => { container.current = node }} className={`glass-surface ${active ? '' : 'bg-white/25'} ${className}`} style={{ borderRadius: radius }}
    data-visual-mode={mode} data-glass-renderer={renderer}>
    {renderer === 'svg' && <div className="glass-effect" aria-hidden="true">
      {/* 折射、模糊、饱和度、色散均使用库的默认值，与官方样例一致；只关闭弹性位移，保证内容层不跟着晃动。 */}
      <LiquidGlass key={sizeKey} className="glass-library" mode="standard" cornerRadius={radius}
        elasticity={0} padding="0" mouseContainer={container}
        style={{ position: 'absolute', inset: '0', top: '0px', left: '0px', width: '100%', height: '100%' }}>
        <span />
      </LiquidGlass>
    </div>}
    <Content className="glass-content">{children}</Content>
  </Element>
}
