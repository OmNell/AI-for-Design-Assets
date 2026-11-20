import { useId, useState, type PropsWithChildren } from 'react'
import { Icon } from './icons'
import { IconButton } from './primitives'
import { Dialog } from './Dialog'

export function ContextPane({ title, children, onClose }: PropsWithChildren<{ title: string; onClose?: () => void }>) {
  const [collapsed, setCollapsed] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const id = useId()
  const toggle = () => onClose ? onClose() : setCollapsed(true)
  return <>
    <aside aria-label={title} className={`hidden min-h-0 min-w-0 self-start xl:sticky xl:top-0 xl:block ${collapsed ? 'xl:w-11' : 'xl:w-[320px] 2xl:w-[340px]'}`}>
      {collapsed ? <IconButton label={`展开${title}`} aria-expanded={false} onClick={() => setCollapsed(false)}><Icon name="panel" /></IconButton> :
        <div className="ui-panel flex max-h-[calc(100dvh-7rem)] flex-col">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
            <h2 className="font-semibold text-slate-900">{title}</h2>
            <IconButton label={`收起${title}`} aria-expanded={true} aria-controls={id} onClick={toggle}><Icon name="panel" /></IconButton>
          </div>
          <div id={id} className="min-w-0 overflow-y-auto break-words p-4">{children}</div>
        </div>}
    </aside>
    <div className="fixed bottom-5 right-5 z-20 shadow-sm xl:hidden">
      <IconButton label={`打开${title}`} onClick={() => setDrawer(true)}><Icon name="panel" /></IconButton>
      <Dialog open={drawer} title={title} onClose={() => setDrawer(false)}>{children}</Dialog>
    </div>
  </>
}
