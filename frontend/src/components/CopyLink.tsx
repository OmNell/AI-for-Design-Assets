import { useState } from 'react'
import { linkUrl } from '../api/catalog'

/** 复制永久链接；浏览器不允许写剪贴板时把链接显示出来让人手动复制。 */
export function CopyLink({ linkId, label }: { linkId: string | null; label: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'manual'>('idle')
  if (!linkId) return null
  const url = linkUrl(linkId)
  const copy = async () => {
    try { await navigator.clipboard.writeText(url); setState('copied'); setTimeout(() => setState('idle'), 2000) } catch { setState('manual') }
  }
  return <span className="inline-flex items-center gap-2"><button type="button" className="rounded-full border border-white/70 bg-white/50 px-3 py-1 text-xs hover:bg-white/80" onClick={() => void copy()}>{state === 'copied' ? '已复制' : label}</button>
    {state === 'manual' && <input readOnly aria-label={`${label}地址`} className="field !w-72 !rounded-xl !px-2 !py-1 text-xs" value={url} onFocus={event => event.target.select()} />}</span>
}
