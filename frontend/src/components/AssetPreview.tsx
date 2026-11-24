import { Button } from './ui'
import { useEffect, useState } from 'react'
import { ApiError } from '../api/client'
import { getPreview } from '../api/catalog'
import { ZoomImage } from './review/ImageViewer'
import type { ThumbState } from './review/Thumb'

export function AssetPreview({ fileId, name, warning, verifiedOnly = false, compact = false, stagedUrl }: {
  fileId: number; name: string; warning?: string | null; verifiedOnly?: boolean; compact?: boolean; stagedUrl?: string
}) {
  const [image, setImage] = useState<ThumbState>({ status: 'pending', message: '正在读取图片…' })
  const [allow, setAllow] = useState(false)
  const [changed, setChanged] = useState(false)
  const [transfer] = useState(() => Array.from(crypto.getRandomValues(new Uint8Array(12)), byte => byte.toString(16).padStart(2, '0')).join(''))
  useEffect(() => {
    if (warning && !allow) return
    const abort = new AbortController()
    let objectUrl = ''
    setImage({ status: 'pending', message: '正在读取图片…' })
    getPreview(stagedUrl ?? `/api/files/${fileId}/preview?transfer=${transfer}&allow_changed=${allow}`, abort.signal)
      .then(async response => {
        const blob = await response.blob()
        if (abort.signal.aborted) return
        setChanged(response.headers.has('X-Asset-Warning'))
        objectUrl = URL.createObjectURL(blob)
        setImage({ status: 'ready', url: objectUrl })
      })
      .catch((reason: unknown) => {
        if (abort.signal.aborted) return
        setChanged(reason instanceof ApiError && reason.code === 'content_changed')
        setImage({ status: 'failed', message: reason instanceof Error ? reason.message : '无法读取图片' })
      })
    return () => { abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [fileId, allow, transfer, warning, stagedUrl])
  const showWarning = warning || changed
  return <div className="space-y-3">
    {showWarning && <div role="alert" className="rounded-xl bg-amber-100 p-3 text-sm text-amber-900">
      <p>{warning || 'NAS 上的内容已经变化，当前图片未经审核确认。'}</p>
      {!allow && !verifiedOnly && <Button primary={false} className="mt-2" onClick={() => setAllow(true)}>查看当前 NAS 图片</Button>}
    </div>}
    {(!showWarning || allow) && <div className={compact ? "h-[min(42vh,380px)] bg-slate-50" : "h-[min(64vh,670px)] bg-slate-50"}>
      <ZoomImage key={fileId} image={image} name={name} />
    </div>}
    {!stagedUrl && (!showWarning || allow) && image.status === 'ready' && <a className="inline-block text-sm text-blue-700 underline" target="_blank" rel="noreferrer"
      href={`/api/files/${fileId}/preview?original=true&transfer=${transfer}&allow_changed=${allow}`}>打开原图</a>}
  </div>
}
