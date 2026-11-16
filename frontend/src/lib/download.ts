import { ApiError } from '../api/client'
import { checkFile, downloadMany } from '../api/catalog'

/** 把下载失败的原因说清楚：NAS 无权限、文件已不存在、NAS 连接超时需重新登录。 */
export function downloadMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'nas_permission_denied' || error.status === 403) return 'NAS 无权限：你的 NAS 账号不能读取这个文件，请联系管理员在 NAS 上授权。'
    if (error.code === 'file_missing' || error.code === 'file_deleted' || error.code === 'file_not_found') return '文件已不存在：在 NAS 上找不到这个文件，已记入待巡检。'
    if (error.status === 401) return 'NAS 连接已超时，请重新登录后再下载。'
    if (error.code === 'nas_unavailable' || error.status === 503) return 'NAS 暂时连不上，请稍后重试。'
    return error.message
  }
  return '下载没有开始，请重试。'
}

/** 交给浏览器自己下载（支持断点续传），不把文件内容读进页面内存。 */
export function triggerDownload(url: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = ''
  anchor.style.display = 'none'
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
}

export interface BatchProgress { done: number; total: number; current: string; failures: { name: string; reason: string }[] }

/** 先探测权限与是否存在，再逐个触发浏览器下载；任何一个失败都不影响后面的文件。 */
export async function downloadFiles(ids: number[], onProgress: (progress: BatchProgress) => void, pause = 700): Promise<BatchProgress> {
  const items = await downloadMany(ids)
  const progress: BatchProgress = { done: 0, total: items.length, current: '', failures: [] }
  for (const item of items) {
    progress.current = item.name
    onProgress({ ...progress })
    try {
      await checkFile(item.file_id)
      triggerDownload(item.url)
      // 给浏览器留出启动上一个下载的时间，避免连续触发被拦截。
      if (pause) await new Promise(resolve => setTimeout(resolve, pause))
    } catch (error: unknown) {
      progress.failures = [...progress.failures, { name: item.name, reason: downloadMessage(error) }]
      if (error instanceof ApiError && error.status === 401) { progress.done++; break }
    }
    progress.done++
    onProgress({ ...progress })
  }
  progress.current = ''
  onProgress({ ...progress })
  return progress
}
