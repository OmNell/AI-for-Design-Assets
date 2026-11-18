import { useEffect, useRef, useState } from 'react'
import { getThumbnail, type ThumbnailSize } from '../../api/thumbnails'

export type ThumbState = { status: 'ready'; url: string } | { status: 'pending' | 'failed'; message: string }

/** 读取登录后才能访问的缓存图；生成中每秒重试，卸载或换图时释放 Blob。 */
export function useThumbnail(batchId: number, fileId: number, size: ThumbnailSize, version: string, enabled = true): ThumbState {
  const [state, setState] = useState<ThumbState>({ status: 'pending', message: '缩略图生成中' })
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let objectUrl: string | null = null
    setState({ status: 'pending', message: '缩略图生成中' })
    const load = async () => {
      try {
        const result = await getThumbnail(batchId, fileId, size, controller.signal)
        if (controller.signal.aborted) return
        if (result.status === 'ready') {
          objectUrl = URL.createObjectURL(result.blob)
          setState({ status: 'ready', url: objectUrl })
        } else {
          setState(result)
          if (result.status === 'pending') timer = setTimeout(() => { void load() }, 1000)
        }
      } catch {
        if (!controller.signal.aborted) timer = setTimeout(() => { void load() }, 1000)
      }
    }
    void load()
    return () => {
      controller.abort()
      if (timer !== undefined) clearTimeout(timer)
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [batchId, fileId, size, version, enabled])
  return state
}

/** 进入视口后才请求，60 张以上的列表不会一次发出全部图片请求。 */
function useInView<T extends Element>(): [React.RefObject<T>, boolean] {
  const ref = useRef<T>(null)
  const [seen, setSeen] = useState(typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    const element = ref.current
    if (seen || !element) return
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setSeen(true); observer.disconnect() }
    }, { rootMargin: '200px' })
    observer.observe(element)
    return () => observer.disconnect()
  }, [seen])
  return [ref, seen]
}

interface Props { batchId: number; fileId: number; version: string; size?: ThumbnailSize; alt: string; className?: string }
export function Thumb({ batchId, fileId, version, size = 300, alt, className = 'h-12 w-12' }: Props) {
  const [ref, seen] = useInView<HTMLSpanElement>()
  const state = useThumbnail(batchId, fileId, size, version, seen)
  return <span ref={ref} className={`flex shrink-0 items-center justify-center overflow-hidden rounded-lg bg-white/40 text-center text-[10px] text-slate-500 ${className}`}>
    {state.status === 'ready' ? <img src={state.url} alt={alt} draggable={false} decoding="async" className="h-full w-full object-contain" /> :
      <span title={state.message}>{state.status === 'failed' ? '无缩略图' : '生成中'}</span>}
  </span>
}
