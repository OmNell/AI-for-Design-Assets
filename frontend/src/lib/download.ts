import { checkFile, downloadMany, getDelivery } from '../api/catalog'

export const describeError = (reason: unknown) => reason instanceof Error ? reason.message : '下载未完成，请重试'

export function triggerDownload(url: string) {
  const address = new URL(url, window.location.origin)
  if (address.origin !== window.location.origin || !/^\/api\/files\/\d+\/download$/.test(address.pathname)) {
    throw new Error('下载地址无效')
  }
  const anchor = document.createElement('a')
  anchor.href = address.href
  anchor.download = ''
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
}

export interface BatchTransfer { total: number; sent: number; failed: { name: string; message: string }[]; current: string }
const wait = (duration: number) => new Promise(resolve => setTimeout(resolve, duration))

async function watch(fileId: number, url: string) {
  const key = new URL(url, window.location.origin).searchParams.get('transfer')
  if (!key) throw new Error('工作台未返回下载标识')
  let retries = 0
  while (true) {
    const result = await getDelivery(fileId, key)
    if (result.status === 'sent') return
    if (result.status === 'interrupted') throw new Error('传输已中断，可在浏览器下载列表续传')
    if (result.status === 'failed') throw new Error(result.message ?? '文件下载失败')
    if (result.status === 'pending' && ++retries >= 240) throw new Error('未收到下载请求，请检查浏览器的自动下载权限')
    await wait(1500)
  }
}

export async function sequentialDownload(ids: number[], update: (progress: BatchTransfer) => void) {
  let state: BatchTransfer = { total: ids.length, sent: 0, failed: [], current: '' }
  update(state)
  const queue = await downloadMany(ids)
  for (const row of queue) {
    const name = row.name ?? `文件 #${row.file_id}`
    update({ ...state, current: name })
    try {
      if (!row.url) throw new Error(row.error ?? '文件无法下载')
      triggerDownload(row.url)
      await watch(row.file_id, row.url)
      state = { ...state, sent: state.sent + 1 }
    } catch (reason) {
      state = { ...state, failed: [...state.failed, { name, message: describeError(reason) }] }
    }
    update(state)
  }
}

export async function downloadFile(id: number) {
  const info = await checkFile(id)
  if (info.warning && !window.confirm(`${info.warning}\n仍要下载当前 NAS 文件？`)) return
  triggerDownload(info.url)
}
