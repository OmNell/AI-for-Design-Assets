import { useRef, useState } from 'react'
import { GlassButton } from './glass/GlassButton'

export function CopyLink({ url, label = '复制永久链接' }: { url: string | null; label?: string }) {
  const [status, setStatus] = useState('')
  const field = useRef<HTMLInputElement>(null)
  if (!url) return null
  const copy = async () => {
    try {
      if (navigator.clipboard) await navigator.clipboard.writeText(url)
      else {
        field.current?.focus()
        field.current?.select()
        if (!document.execCommand('copy')) throw new Error()
      }
      setStatus('已复制')
    } catch {
      setStatus('请手动复制链接')
      requestAnimationFrame(() => field.current?.select())
    }
  }
  return <span className="inline-flex flex-wrap items-center gap-2">
    <GlassButton primary={false} onClick={() => void copy()}>{label}</GlassButton>
    <input aria-label={label + '地址'} ref={field} readOnly value={url} className={status === '请手动复制链接' ? 'field w-72 text-xs' : 'sr-only'} tabIndex={status === '请手动复制链接' ? 0 : -1} />
    {status && <span role="status" className="text-xs text-emerald-800">{status}</span>}
  </span>
}
