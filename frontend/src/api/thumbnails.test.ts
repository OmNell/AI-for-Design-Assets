import { afterEach, expect, it, vi } from 'vitest'
import { getThumbnail } from './thumbnails'
import { useAuthStore } from '../stores/auth'

afterEach(() => { vi.unstubAllGlobals(); useAuthStore.getState().clear() })
const response = (detail: unknown, status = 404) => new Response(JSON.stringify({ detail }), { status })

it('缓存图片请求只携带本人 Cookie，并返回 JPEG Blob', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(new Blob(['jpeg'], { type: 'image/jpeg' })))
  vi.stubGlobal('fetch', fetcher)
  const signal = new AbortController().signal
  const result = await getThumbnail(2, 3, 300, signal)
  expect(fetcher).toHaveBeenCalledWith('/api/batches/2/thumbs/3/300', { credentials: 'same-origin', cache: 'no-store', signal })
  expect(result.status).toBe('ready')
  if (result.status === 'ready') expect(result.blob.type).toBe('image/jpeg')
})

it('未生成的 404 可重试；损坏文件的失败原因保留', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ retryable: true })).mockResolvedValueOnce(response({ retryable: false, reason: '图片损坏或无法解码' })))
  const signal = new AbortController().signal
  expect(await getThumbnail(1, 1, 300, signal)).toMatchObject({ status: 'pending' })
  expect(await getThumbnail(1, 1, 300, signal)).toEqual({ status: 'failed', message: '图片损坏或无法解码' })
})

it('Cookie 到期仅提示图片不可见，不清除正在上传的登录状态', async () => {
  useAuthStore.setState({ user: { id: 1, username: 'demo', display_name: null, enabled: true, roles: ['uploader'] }, initialized: true, notice: '' })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(null, 401)))
  expect(await getThumbnail(1, 1, 300, new AbortController().signal)).toEqual({ status: 'failed', message: '登录后可查看缩略图' })
  expect(useAuthStore.getState().user?.username).toBe('demo')
})

it('网络/服务异常继续重试，请求取消向调用方传播', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(response(null, 503)))
  const controller = new AbortController()
  expect(await getThumbnail(1, 1, 300, controller.signal)).toMatchObject({ status: 'pending' })
  expect(await getThumbnail(1, 1, 300, controller.signal)).toMatchObject({ status: 'pending' })
  controller.abort()
  const abort = new DOMException('已取消', 'AbortError')
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abort))
  await expect(getThumbnail(1, 1, 300, controller.signal)).rejects.toBe(abort)
})
