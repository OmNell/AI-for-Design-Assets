import { useEffect, useState, type ButtonHTMLAttributes, type InputHTMLAttributes, type PropsWithChildren, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react'
import { Icon } from './icons'

export function Button({ children, className = '', primary = true, loading = false, disabled, type = 'button', ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean; loading?: boolean }) {
  return <button {...props} type={type} disabled={disabled || loading} aria-busy={loading || undefined}
    className={`ui-button ${primary ? 'ui-primary' : 'ui-secondary'} ${className}`}>
    {loading && <Icon name="progress" className="animate-spin" />}{children}
  </button>
}

export function IconButton({ children, label, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <Button {...props} primary={false} aria-label={label} title={label} className={`!h-8 !w-8 !p-0 ${className}`}>{children}</Button>
}

export const Input = ({ className = '', ...props }: InputHTMLAttributes<HTMLInputElement>) => <input className={`field ${className}`} {...props} />
export const Select = ({ className = '', ...props }: SelectHTMLAttributes<HTMLSelectElement>) => <select className={`field ${className}`} {...props} />
export const Textarea = ({ className = '', ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea className={`field ${className}`} {...props} />
export const Checkbox = (props: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>) => <input {...props} type="checkbox" />

const tones = {
  neutral: 'bg-slate-100 text-slate-600', info: 'bg-blue-50 text-blue-700',
  success: 'bg-emerald-50 text-emerald-700', warning: 'bg-amber-50 text-amber-800', danger: 'bg-red-50 text-red-700',
}
export function Badge({ children, tone = 'neutral' }: PropsWithChildren<{ tone?: keyof typeof tones }>) {
  return <span className={`inline-flex items-center whitespace-nowrap rounded-md px-2 py-1 text-xs font-medium ${tones[tone]}`}>{children}</span>
}

export function Panel({ children, className = '' }: PropsWithChildren<{ className?: string }>) {
  return <section className={`ui-panel p-5 ${className}`}>{children}</section>
}

export function Card({ children, className = '' }: PropsWithChildren<{ className?: string }>) {
  return <div className={`ui-panel p-4 ${className}`}>{children}</div>
}

export function ListItem({ children, className = '', active = false }: PropsWithChildren<{ className?: string; active?: boolean }>) {
  return <div className={`min-w-0 rounded-lg border ${active ? 'border-blue-300 bg-blue-50' : 'border-slate-200 bg-white'} ${className}`}>{children}</div>
}

export function Table({ children, label }: PropsWithChildren<{ label?: string }>) {
  return <div className="overflow-x-auto"><table aria-label={label} className="ui-table">{children}</table></div>
}

export function EmptyState({ title, children }: PropsWithChildren<{ title: string }>) {
  return <div className="grid justify-items-center gap-2 px-4 py-9 text-center">
    <span className="rounded-lg bg-slate-100 p-2 text-slate-500"><Icon name="inbox" className="size-5" /></span>
    <p className="font-medium text-slate-700">{title}</p>{children && <div className="text-sm text-slate-500">{children}</div>}
  </div>
}
export const LoadingState = ({ children = '正在加载…' }: PropsWithChildren) =>
  <p role="status" className="flex items-center gap-2 p-4 text-sm text-slate-500"><Icon name="progress" className="animate-spin" />{children}</p>
export const ErrorState = ({ children, retry }: PropsWithChildren<{ retry?: () => void }>) =>
  <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">
    <span className="min-w-0 break-words">{children}</span>{retry && <Button primary={false} onClick={retry}>重试</Button>}
  </div>

export function Pagination({ page, pages, total, onChange }: { page: number; pages: number; total: number; onChange: (page: number) => void }) {
  return <nav aria-label="分页" className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 px-4 py-3 text-xs text-slate-500">
    <span>共 {total} 项 · 第 {page} / {pages} 页</span><div className="flex gap-2">
      <Button primary={false} disabled={page <= 1} onClick={() => onChange(page - 1)}>上一页</Button>
      <Button primary={false} disabled={page >= pages} onClick={() => onChange(page + 1)}>下一页</Button>
    </div>
  </nav>
}
export function Breadcrumb({ children }: PropsWithChildren) {
  return <nav aria-label="当前位置" className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-slate-500">{children}</nav>
}

export function ViewSwitch({ view, onChange }: { view: 'list' | 'grid'; onChange: (value: 'list' | 'grid') => void }) {
  return <div role="group" aria-label="文件视图" className="inline-flex shrink-0 gap-1">
    <IconButton label="列表视图" aria-pressed={view === 'list'} onClick={() => onChange('list')}><Icon name="list" /></IconButton>
    <IconButton label="缩略图视图" aria-pressed={view === 'grid'} onClick={() => onChange('grid')}><Icon name="grid" /></IconButton>
  </div>
}

export function FileIcon({ name, folder = false }: { name?: string; folder?: boolean }) {
  const ext = name?.includes('.') ? name.split('.').at(-1)?.toUpperCase().slice(0, 5) : undefined
  return <span aria-hidden="true" className={`inline-flex shrink-0 items-center justify-center rounded-md p-2 ${folder ? 'bg-blue-50 text-blue-700' : 'bg-slate-100 text-slate-500'}`}>
    <Icon name={folder ? 'folder' : ext && ['PNG', 'JPG', 'JPEG'].includes(ext) ? 'image' : 'file'} />
  </span>
}

export function FileThumbnail({ src, name }: { src?: string; name: string }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [src])
  return <div className="grid aspect-[4/3] place-items-center overflow-hidden rounded-md bg-slate-100">
    {src && !failed ? <img src={src} alt="" loading="lazy" onError={() => setFailed(true)} className="size-full object-contain" /> : <FileIcon name={name} />}
  </div>
}
