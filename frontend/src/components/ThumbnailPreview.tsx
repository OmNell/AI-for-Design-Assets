import { useEffect, useState } from 'react'
import { getThumbnail } from '../api/thumbnails'
import type { UploadFile } from '../api/upload'

type PreviewState = { status: 'ready'; url: string } | { status: 'pending' | 'failed'; message: string }

export function ThumbnailPreview({ batchId, file }: { batchId: number; file: UploadFile }) {
  const [state, setState] = useState<PreviewState>({ status: 'pending', message: '缩略图生成中' })
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let objectUrl: string | null = null
    setState({ status: 'pending', message: '缩略图生成中' })
    const load = async () => {
      try {
        const result = await getThumbnail(batchId, file.id, 300, controller.signal)
        if (controller.signal.aborted) return
        if (result.status === 'ready') {
          objectUrl = URL.createObjectURL(result.blob)
          setState({ status: 'ready', url: objectUrl })
        } else {
          setState(result)
          if (result.status === 'pending') timer = setTimeout(() => { void load() }, 1000)
        }
      } catch (error: unknown) {
        if (!controller.signal.aborted) {
          setState({ status: 'pending', message: '缩略图暂不可用，正在重试' })
          timer = setTimeout(() => { void load() }, 1000)
        }
      }
    }
    if (file.thumb_status === 'failed') setState({ status: 'failed', message: file.thumb_error || '缩略图生成失败' })
    else void load()
    return () => {
      controller.abort()
      if (timer !== undefined) clearTimeout(timer)
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [batchId, file.id, file.fingerprint, file.thumb_status, file.thumb_error])

  return <div className="flex max-w-64 shrink-0 items-center gap-3">
    {state.status === 'ready' ? <img src={state.url} alt={`${file.rel_path} 的缩略图`} width={64} height={64} className="h-16 w-16 rounded-xl bg-white/30 object-contain" onError={() => setState({ status: 'failed', message: '缩略图无法显示，请刷新重试' })} /> :
      <span className={`flex min-h-16 w-16 shrink-0 items-center justify-center rounded-xl bg-white/25 p-2 text-center text-xs ${state.status === 'failed' ? 'text-amber-800' : 'text-slate-500'}`} aria-hidden="true">{state.status === 'failed' ? '缩略图失败' : '生成中'}</span>}
    {state.status !== 'ready' && <p role="status" className={`max-w-40 text-xs ${state.status === 'failed' ? 'text-amber-800' : 'text-slate-500'}`}>{state.message}</p>}
  </div>
}
