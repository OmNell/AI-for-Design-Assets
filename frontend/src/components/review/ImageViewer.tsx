import { useEffect, useRef, useState } from 'react'
import { useThumbnail, type ThumbState } from './Thumb'

interface View { scale: number; x: number; y: number }
const INITIAL: View = { scale: 1, x: 0, y: 0 }
const clamp = (value: number) => Math.min(8, Math.max(1, value))

export interface ViewerPin { x: number; y: number; text: string }
interface Props { batchId: number; fileId: number; version: string; name: string; pins?: ViewerPin[]; onPin?: (x: number, y: number) => void }

/** 大图查看：滚轮以指针为中心缩放，按住拖动，双击还原；可显示钉在图上的意见，审核时可点击添加。 */
export function ImageViewer({ batchId, fileId, version, name, pins, onPin }: Props) {
  const image = useThumbnail(batchId, fileId, 1600, version)
  return <ZoomImage key={`${fileId}:${version}`} image={image} name={name} pins={pins} onPin={onPin} />
}

interface ZoomProps { image: ThumbState; name: string; pins?: ViewerPin[]; onPin?: (x: number, y: number) => void }
export function ZoomImage({ image, name, pins = [], onPin }: ZoomProps) {
  const picture = useRef<HTMLImageElement>(null)
  const [pinning, setPinning] = useState(false)
  const [box, setBox] = useState({ left: 0, top: 0, width: 0, height: 0 })
  const frame = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number } | null>(null)
  const [view, setView] = useState<View>(INITIAL)

  useEffect(() => {
    const element = frame.current
    if (!element) return
    // React 的 wheel 监听是被动的，无法阻止页面跟着滚动。
    const wheel = (event: WheelEvent) => {
      event.preventDefault()
      const box = element.getBoundingClientRect()
      const cx = event.clientX - box.left - box.width / 2
      const cy = event.clientY - box.top - box.height / 2
      setView(current => {
        const scale = clamp(current.scale * Math.exp(-event.deltaY * 0.0015))
        if (scale === 1) return INITIAL
        const ratio = scale / current.scale
        return { scale, x: cx - (cx - current.x) * ratio, y: cy - (cy - current.y) * ratio }
      })
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [])
  const measure = () => {
    // 记录图片在画框里的原始位置（不含缩放平移），钉点覆盖层与图片使用同一个变换。
    const element = picture.current
    if (element) setBox({ left: element.offsetLeft, top: element.offsetTop, width: element.offsetWidth, height: element.offsetHeight })
  }
  useEffect(() => {
    const element = frame.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const place = (event: React.MouseEvent) => {
    const element = picture.current
    if (!pinning || !onPin || !element) return
    const rect = element.getBoundingClientRect()
    const x = (event.clientX - rect.left) / rect.width, y = (event.clientY - rect.top) / rect.height
    if (x >= 0 && x <= 1 && y >= 0 && y <= 1) { onPin(Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000); setPinning(false) }
  }
  const zoom = (factor: number) => setView(current => {
    const scale = clamp(current.scale * factor)
    return scale === 1 ? INITIAL : { scale, x: current.x * scale / current.scale, y: current.y * scale / current.scale }
  })
  return <div className="relative h-full min-h-0 w-full">
    <div ref={frame} data-testid="viewer" data-scale={view.scale.toFixed(2)} className={`flex h-full w-full touch-none select-none items-center justify-center overflow-hidden rounded-2xl bg-slate-50 ${pinning ? 'cursor-crosshair' : view.scale > 1 ? 'cursor-grab active:cursor-grabbing' : ''}`} onClick={place}
      onPointerDown={event => { if (view.scale > 1 && !pinning) { drag.current = { x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture?.(event.pointerId) } }}
      onPointerMove={event => {
        const start = drag.current
        if (!start) return
        const dx = event.clientX - start.x, dy = event.clientY - start.y
        drag.current = { x: event.clientX, y: event.clientY }
        setView(current => ({ ...current, x: current.x + dx, y: current.y + dy }))
      }}
      onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }} onDoubleClick={() => { if (!pinning) setView(INITIAL) }}>
      {image.status === 'ready' ? <img ref={picture} onLoad={measure} src={image.url} alt={`${name} 大图`} draggable={false} className="max-h-full max-w-full object-contain will-change-transform"
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }} /> :
        <p role="status" className={`text-sm ${image.status === 'failed' ? 'text-red-800' : 'text-slate-500'}`}>{image.message}</p>}
      {image.status === 'ready' && !!pins.length && <div className="pointer-events-none absolute inset-0" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }}>
        {pins.map((pin, index) => <span key={index} data-testid="pin" title={pin.text} className="pointer-events-auto absolute flex h-6 w-6 items-center justify-center rounded-full border-2 border-white bg-red-600 text-xs font-semibold text-white shadow"
          style={{ left: box.left + pin.x * box.width, top: box.top + pin.y * box.height, transform: `translate(-50%, -50%) scale(${1 / view.scale})` }}>{index + 1}</span>)}</div>}
    </div>
    {onPin && <button type="button" aria-pressed={pinning} className={`absolute bottom-3 left-3 rounded-full px-3 py-1 text-xs ${pinning ? 'bg-red-600 text-white' : 'bg-white text-slate-700 hover:bg-white'}`}
      onClick={() => setPinning(value => !value)}>{pinning ? '点击图片上的位置…' : '钉意见'}</button>}
    <div className="absolute bottom-3 right-3 flex items-center gap-1 rounded-full bg-white px-2 py-1 text-xs text-slate-700">
      <button type="button" aria-label="缩小" className="h-6 w-6 rounded-full hover:bg-white" onClick={() => zoom(1 / 1.4)}>－</button>
      <span className="w-10 text-center tabular-nums">{Math.round(view.scale * 100)}%</span>
      <button type="button" aria-label="放大" className="h-6 w-6 rounded-full hover:bg-white" onClick={() => zoom(1.4)}>＋</button>
      <button type="button" className="rounded-full px-2 py-0.5 hover:bg-white" onClick={() => setView(INITIAL)}>还原</button>
    </div>
  </div>
}
