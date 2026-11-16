// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ThumbnailPreview } from './ThumbnailPreview'
import { getThumbnail } from '../api/thumbnails'
import type { UploadFile } from '../api/upload'

vi.mock('../api/thumbnails', () => ({ getThumbnail: vi.fn() }))
const file: UploadFile = { id: 1, rel_path: '单品/主图.jpg', size: 10, mtime: 1, upload_status: 'done', bytes_received: 10, fingerprint: 'first', thumb_status: 'pending', thumb_error: null }
let root: Root
let host: HTMLDivElement
const createUrl = vi.fn(() => 'blob:thumbnail')
const revokeUrl = vi.fn()
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createUrl })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeUrl })
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
})
afterEach(() => {
  act(() => root.unmount()); host.remove()
  vi.useRealTimers(); vi.resetAllMocks(); vi.unstubAllGlobals()
  createUrl.mockReturnValue('blob:thumbnail')
})

it('未生成时轮询，生成后显示图片并停止；卸载释放缓存 URL', async () => {
  vi.mocked(getThumbnail).mockResolvedValueOnce({ status: 'pending', message: '缩略图生成中' }).mockResolvedValueOnce({ status: 'ready', blob: new Blob(['jpeg']) })
  await act(async () => root.render(<ThumbnailPreview batchId={2} file={file} />))
  expect(host.textContent).toContain('缩略图生成中')
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(host.querySelector('img')?.getAttribute('src')).toBe('blob:thumbnail')
  expect(host.querySelector('img')?.alt).toContain('单品/主图.jpg')
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(getThumbnail).toHaveBeenCalledTimes(2)
  act(() => root.render(null))
  expect(revokeUrl).toHaveBeenCalledWith('blob:thumbnail')
})

it('损坏图片显示失败原因，不再轮询，已有失败元数据无需请求', async () => {
  vi.mocked(getThumbnail).mockResolvedValue({ status: 'failed', message: '图片损坏或无法解码' })
  await act(async () => root.render(<ThumbnailPreview batchId={2} file={file} />))
  expect(host.textContent).toContain('图片损坏或无法解码')
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(getThumbnail).toHaveBeenCalledTimes(1)
  act(() => root.render(null))
  await act(async () => root.render(<ThumbnailPreview batchId={2} file={{ ...file, thumb_status: 'failed', thumb_error: '缩略图处理失败' }} />))
  expect(host.textContent).toContain('缩略图处理失败')
  expect(getThumbnail).toHaveBeenCalledTimes(1)
})

it('卸载会取消在途请求，防止取消批次后出现迟到图片', async () => {
  vi.mocked(getThumbnail).mockImplementation(() => new Promise(() => undefined))
  await act(async () => root.render(<ThumbnailPreview batchId={2} file={file} />))
  const signal = vi.mocked(getThumbnail).mock.calls[0][3]
  expect(signal.aborted).toBe(false)
  act(() => root.render(null))
  expect(signal.aborted).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
  expect(getThumbnail).toHaveBeenCalledTimes(1)
})

it('同一文件 ID 重新上传后指纹变化，释放旧图片并获取新图片', async () => {
  vi.mocked(getThumbnail).mockResolvedValueOnce({ status: 'ready', blob: new Blob(['first']) }).mockResolvedValueOnce({ status: 'ready', blob: new Blob(['second']) })
  createUrl.mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:second')
  await act(async () => root.render(<ThumbnailPreview batchId={2} file={file} />))
  expect(host.querySelector('img')?.getAttribute('src')).toBe('blob:first')
  await act(async () => root.render(<ThumbnailPreview batchId={2} file={{ ...file, fingerprint: 'second' }} />))
  expect(revokeUrl).toHaveBeenCalledWith('blob:first')
  expect(host.querySelector('img')?.getAttribute('src')).toBe('blob:second')
  expect(getThumbnail).toHaveBeenCalledTimes(2)
})
