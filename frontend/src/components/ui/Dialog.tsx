import { useEffect, useId, useRef, type PropsWithChildren } from 'react'
import { createPortal } from 'react-dom'
import { Button } from './primitives'

export function Dialog({ open, title, onClose, children }: PropsWithChildren<{ open: boolean; title: string; onClose: () => void }>) {
  const dialog = useRef<HTMLDialogElement>(null)
  const label = useId()
  useEffect(() => {
    const element = dialog.current
    const trigger = document.activeElement
    if (open && element && !element.open) element.showModal()
    return () => {
      if (element?.open) element.close()
      if (open && trigger instanceof HTMLElement && trigger.isConnected) trigger.focus()
    }
  }, [open])
  if (!open) return null
  return createPortal(<dialog ref={dialog} aria-labelledby={label} onCancel={event => { event.preventDefault(); onClose() }}
    className="ui-dialog m-auto overflow-auto rounded-xl border border-slate-200 bg-white text-slate-900 shadow-lg backdrop:bg-slate-900/40">
    <h2 id={label} className="mb-5 text-xl font-semibold">{title}</h2>{children}
    <div className="mt-6 flex justify-end"><Button primary={false} onClick={onClose}>关闭</Button></div>
  </dialog>, document.body)
}
