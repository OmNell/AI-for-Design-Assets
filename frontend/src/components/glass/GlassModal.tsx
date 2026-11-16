import { useEffect, useId, useRef, type PropsWithChildren } from 'react'
import { createPortal } from 'react-dom'
import { GlassPanel } from './GlassPanel'
import { GlassButton } from './GlassButton'

export function GlassModal({ open, title, onClose, children }: PropsWithChildren<{ open: boolean; title: string; onClose: () => void }>) {
  const dialog = useRef<HTMLDialogElement>(null)
  const label = useId()
  useEffect(() => {
    const element = dialog.current
    if (open) element?.showModal()
    return () => element?.close()
  }, [open])
  if (!open) return null
  return createPortal(<dialog ref={dialog} aria-labelledby={label} onCancel={event => { event.preventDefault(); onClose() }}
    className="w-[560px] overflow-visible border-0 bg-transparent p-0 text-slate-800 backdrop:bg-slate-900/25 backdrop:backdrop-blur-sm">
    <GlassPanel><h2 id={label} className="mb-5 text-xl font-semibold">{title}</h2>{children}
      <div className="mt-7 flex justify-end"><GlassButton onClick={onClose}>关闭</GlassButton></div></GlassPanel>
  </dialog>, document.body)
}
