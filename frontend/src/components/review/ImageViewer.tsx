import { useEffect, useRef, useState } from 'react'
import { useThumbnail } from './Thumb'

interface View { scale: number; x: number; y: number }
const INITIAL: View = { scale: 1, x: 0, y: 0 }
const clamp = (value: number) => Math.min(8, Math.max(1, value))

/** 大图查看：滚轮以指针为中心缩放，按住拖动，双击还原。 */
export function ImageViewer({ batchId, fileId, version, name }: { batchId: number; fileId: number; version: string; name: string }) {
  const image = useThumbnail(batchId, fileId, 1600, version)
  const frame = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x: number; y: number } | null>(null)
  const [view, setView] = useState<View>(INITIAL)
  useEffect(() => { setView(INITIAL) }, [fileId, version])
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
  const zoom = (factor: number) => setView(current => {
    const scale = clamp(current.scale * factor)
    return scale === 1 ? INITIAL : { scale, x: current.x * scale / current.scale, y: current.y * scale / current.scale }
  })
  return <div className="relative h-full min-h-0 w-full">
    <div ref={frame} data-testid="viewer" data-scale={view.scale.toFixed(2)} className={`flex h-full w-full touch-none select-none items-center justify-center overflow-hidden rounded-2xl bg-white/30 ${view.scale > 1 ? 'cursor-grab active:cursor-grabbing' : ''}`}
      onPointerDown={event => { if (view.scale > 1) { drag.current = { x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture?.(event.pointerId) } }}
      onPointerMove={event => {
        const start = drag.current
        if (!start) return
        const dx = event.clientX - start.x, dy = event.clientY - start.y
        drag.current = { x: event.clientX, y: event.clientY }
        setView(current => ({ ...current, x: current.x + dx, y: current.y + dy }))
      }}
      onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }} onDoubleClick={() => setView(INITIAL)}>
      {image.status === 'ready' ? <img src={image.url} alt={`${name} 大图`} draggable={false} className="max-h-full max-w-full object-contain will-change-transform"
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }} /> :
        <p role="status" className={`text-sm ${image.status === 'failed' ? 'text-red-800' : 'text-slate-500'}`}>{image.message}</p>}
    </div>
    <div className="absolute bottom-3 right-3 flex items-center gap-1 rounded-full bg-white/70 px-2 py-1 text-xs text-slate-700">
      <button type="button" aria-label="缩小" className="h-6 w-6 rounded-full hover:bg-white" onClick={() => zoom(1 / 1.4)}>－</button>
      <span className="w-10 text-center tabular-nums">{Math.round(view.scale * 100)}%</span>
      <button type="button" aria-label="放大" className="h-6 w-6 rounded-full hover:bg-white" onClick={() => zoom(1.4)}>＋</button>
      <button type="button" className="rounded-full px-2 py-0.5 hover:bg-white" onClick={() => setView(INITIAL)}>还原</button>
    </div>
  </div>
}
