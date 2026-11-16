import { ApiError } from '../api/client'
import { completeFile, getUploadState, sendChunk, type Batch, type UploadFile } from '../api/upload'
import type { LocalEntry } from './dirReader'

export function uploadOrder(a: UploadFile, b: UploadFile): number {
  const main = (value: UploadFile) => /^(套装|单品)\//.test(value.rel_path) ? 0 : 1
  return main(a) - main(b) || a.size - b.size || a.rel_path.localeCompare(b.rel_path)
}
export class Uploader {
  private controller = new AbortController()
  stop() { this.controller.abort() }
  get stopped() { return this.controller.signal.aborted }
  constructor(private batch: Batch, private entries: LocalEntry[], private update: (batch: Batch) => void) { this.originalToken = batch.upload_token ?? '' }
  private async retry<T>(action: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      if (this.stopped) throw new DOMException('已暂停', 'AbortError')
      try { return await action() }
      catch (error: unknown) {
        if (this.stopped || error instanceof ApiError && error.status >= 400 && error.status < 500 || attempt >= 3) throw error
        await new Promise(resolve => setTimeout(resolve, 300 * (attempt + 1)))
      }
    }
  }
  async run(): Promise<Batch> {
    while (this.batch.queued && !this.stopped) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      this.batch = await this.retry(() => getUploadState(this.batch.id, this.batch.upload_token))
      // 状态响应不重发令牌，仍只保留创建/续传时的内存凭据。
      this.batch.upload_token = this.token
      this.update(this.batch)
    }
    if (this.stopped) throw new DOMException('已暂停', 'AbortError')
    const files = [...this.batch.files].sort(uploadOrder)
    let cursor = 0
    const worker = async () => {
      while (cursor < files.length && !this.stopped) {
        const record = files[cursor++]
        if (record.upload_status === 'done') continue
        const local = this.entries.find(e => e.rel_path === record.rel_path)?.file
        if (!local) throw new Error('本地清单缺少文件，请重新拖入')
        let conflicts = 0
        while (record.bytes_received < record.size) {
          const offset = record.bytes_received
          try {
            const result = await this.retry(() => sendChunk(this.batch.id, record.id, Math.floor(offset / this.batch.chunk_bytes), local.slice(offset, Math.min(offset + this.batch.chunk_bytes, local.size)), this.token, this.controller.signal))
            record.bytes_received = result.bytes_received
            conflicts = 0
            record.upload_status = 'partial'
            this.patch(record)
          } catch (error: unknown) {
            if (error instanceof ApiError && error.status === 409 && error.detail && typeof error.detail === 'object' && 'bytes_received' in error.detail && typeof error.detail.bytes_received === 'number') {
              const received = error.detail.bytes_received
              if (received < 0 || received > record.size || received !== record.size && received % this.batch.chunk_bytes !== 0 || ++conflicts > 3) throw new Error('服务端偏移无法对齐，请重新继续上传')
              record.bytes_received = received
              this.patch(record)
            } else throw error
          }
        }
        const done = await this.retry(() => completeFile(this.batch.id, record.id, this.token))
        // 并行请求的快照可能较旧；只合并该文件，不回退其他文件的字节进度。
        const completed = done.files.find(item => item.id === record.id)!
        Object.assign(record, completed)
        this.patch(record)
        if (done.status === '已暂存') {
          this.batch.status = done.status
          this.batch.upload_token = null
          this.update({ ...this.batch })
        }
      }
    }
    try { await Promise.all(Array.from({ length: this.batch.parallel_files }, worker)) }
    catch (error: unknown) { this.stop(); throw error }
    return this.batch
  }
  private get token(): string { return this.originalToken }
  private originalToken = ''
  private patch(record: UploadFile) {
    this.batch = { ...this.batch, files: this.batch.files.map(item => item.id === record.id ? { ...record } : item) }
    this.update(this.batch)
  }
}
