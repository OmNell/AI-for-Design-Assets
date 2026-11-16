import { afterEach, expect, it, vi } from 'vitest'
import { Uploader, uploadOrder } from './uploader'
import type { Batch } from '../api/upload'
import { ApiError } from '../api/client'
import * as api from '../api/upload'
vi.mock('../api/upload', () => ({ sendChunk: vi.fn(), completeFile: vi.fn(), getUploadState: vi.fn() }))
const batch = (): Batch => ({ id: 1, batch_no: 'B1', folder_name: 'P_企业_项目', original_folder_name: 'P_企业_项目', project_code: 'P', status: '上传中', queued: false, won: true, updated_at: '', directories: [], chunk_bytes: 4, parallel_files: 3, upload_token: 'fake-token', differences: null,
  files: [{ id: 1, rel_path: '单品/a.png', size: 10, mtime: 1, upload_status: 'pending', bytes_received: 0, fingerprint: null }] })
afterEach(() => vi.resetAllMocks())
it('主图优先，其余按大小排序', () => {
  const base = batch().files[0]
  expect([{ ...base, rel_path: 'big', size: 100 }, { ...base, rel_path: 'small', size: 1 }, { ...base, rel_path: '单品/a.png', size: 200 }].sort(uploadOrder).map(e => e.rel_path)).toEqual(['单品/a.png', 'small', 'big'])
})
it('分片顺序、409 对齐、忽略已完成文件，完成状态正常返回', async () => {
  vi.mocked(api.sendChunk).mockRejectedValueOnce(new ApiError(409, 'upload_offset_conflict', '偏移', { bytes_received: 4 })).mockResolvedValueOnce({ bytes_received: 8 }).mockResolvedValueOnce({ bytes_received: 10 })
  vi.mocked(api.completeFile).mockResolvedValue({ ...batch(), status: '已暂存', files: [{ ...batch().files[0], bytes_received: 10, upload_status: 'done' }] })
  const result = await new Uploader(batch(), [{ rel_path: '单品/a.png', is_dir: false, file: new File(['0123456789'], 'a.png') }], () => undefined).run()
  expect(vi.mocked(api.sendChunk).mock.calls.map(args => args[2])).toEqual([0, 1, 2])
  expect(result.status).toBe('已暂存')
  expect(result.upload_token).toBeNull()
  const done = { ...batch(), files: [{ ...batch().files[0], upload_status: 'done' }] }
  await new Uploader(done, [], () => undefined).run()
  expect(api.completeFile).toHaveBeenCalledTimes(1)
})
it('可暂停，不发出后续分片', async () => {
  const job = new Uploader(batch(), [], () => undefined)
  job.stop()
  await expect(job.run()).rejects.toMatchObject({ name: 'AbortError' })
  expect(api.sendChunk).not.toHaveBeenCalled()
})
it('临时错误最多自动重试三次', async () => {
  vi.useFakeTimers()
  vi.mocked(api.sendChunk).mockRejectedValue(new ApiError(503, 'nas_unavailable', '暂时不可用'))
  const result = new Uploader(batch(), [{ rel_path: '单品/a.png', is_dir: false, file: new File(['0123456789'], 'a.png') }], () => undefined).run().catch(error => error)
  await vi.runAllTimersAsync()
  expect(await result).toBeInstanceOf(ApiError)
  expect(api.sendChunk).toHaveBeenCalledTimes(4)
  vi.useRealTimers()
})
