export type ThumbnailSize = 300 | 1600 | 1024
export type ThumbnailResult =
  | { status: 'ready'; blob: Blob }
  | { status: 'pending'; message: string }
  | { status: 'failed'; message: string }

const pending = (): ThumbnailResult => ({ status: 'pending', message: '缩略图生成中' })

// 图片缓存使用登录 Cookie；图片请求失败不改变独立上传任务的登录状态。
export async function getThumbnail(batchId: number, fileId: number, size: ThumbnailSize, signal: AbortSignal): Promise<ThumbnailResult> {
  let response: Response
  try {
    response = await fetch(`/api/batches/${batchId}/thumbs/${fileId}/${size}`, { credentials: 'same-origin', cache: 'no-store', signal })
  } catch (error: unknown) {
    if (signal.aborted) throw error
    return { status: 'pending', message: '缩略图暂不可用，正在重试' }
  }
  if (response.ok) {
    const blob = await response.blob()
    return blob.size ? { status: 'ready', blob } : pending()
  }
  if (response.status === 401) return { status: 'failed', message: '登录后可查看缩略图' }
  if (response.status === 403) return { status: 'failed', message: '无权查看缩略图' }
  const body: unknown = await response.json().catch(() => null)
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null
  if (response.status === 404 && detail && typeof detail === 'object' && 'retryable' in detail && detail.retryable === false) {
    const reason = 'reason' in detail && typeof detail.reason === 'string' ? detail.reason : '缩略图生成失败'
    return { status: 'failed', message: reason }
  }
  if (response.status === 404 || response.status >= 500) return pending()
  return { status: 'failed', message: '缩略图无法显示，请刷新重试' }
}
