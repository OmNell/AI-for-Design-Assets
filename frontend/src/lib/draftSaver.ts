export type SaveStatus = 'saved' | 'dirty' | 'saving' | 'error'

interface Options<P, R> {
  delay: number
  merge: (older: P, newer: P) => P
  send: (id: number, patch: P) => Promise<R>
  /** newer 是请求期间又产生、尚未发送的修改，调用方据此保留界面上的最新输入。 */
  saved: (id: number, result: R, newer: P | undefined) => void
  status: (status: SaveStatus, message?: string) => void
}

/** 修改即保存：按对象合并草稿，防抖后顺序发送；失败的修改留在队列里等待重试。 */
export class DraftSaver<P, R> {
  private queue = new Map<number, P>()
  private timer: ReturnType<typeof setTimeout> | undefined
  private running: Promise<void> | null = null
  private failed = false
  private sending: [number, P] | null = null

  constructor(private options: Options<P, R>) {}

  /** 尚未被服务端确认的修改：正在发送的与排队中的合并。 */
  pending(id: number): P | undefined {
    const sent = this.sending && this.sending[0] === id ? this.sending[1] : undefined
    const queued = this.queue.get(id)
    return sent !== undefined && queued !== undefined ? this.options.merge(sent, queued) : queued ?? sent
  }
  get idle(): boolean { return !this.queue.size && !this.running && !this.failed }

  change(id: number, patch: P): void {
    const existing = this.queue.get(id)
    this.queue.set(id, existing === undefined ? patch : this.options.merge(existing, patch))
    this.options.status(this.running ? 'saving' : 'dirty')
    clearTimeout(this.timer)
    this.timer = setTimeout(() => { void this.flush() }, this.options.delay)
  }

  /** 立即保存全部草稿；全部成功返回 true。 */
  async flush(): Promise<boolean> {
    clearTimeout(this.timer)
    while (this.running) await this.running
    if (this.queue.size) {
      this.running = this.run()
      try { await this.running } finally { this.running = null }
    }
    return !this.failed
  }

  /** 页面关闭时取走未发送的草稿，由调用方用 keepalive 请求送出。 */
  drain(): [number, P][] {
    clearTimeout(this.timer)
    const entries = [...this.queue.entries()]
    this.queue.clear()
    return entries
  }

  dispose(): void { clearTimeout(this.timer) }

  private async run(): Promise<void> {
    this.options.status('saving')
    while (this.queue.size) {
      const [id, patch] = this.queue.entries().next().value as [number, P]
      this.queue.delete(id)
      this.sending = [id, patch]
      try {
        const result = await this.options.send(id, patch)
        this.sending = null
        this.options.saved(id, result, this.queue.get(id))
      } catch (error: unknown) {
        this.sending = null
        const newer = this.queue.get(id)
        this.queue.set(id, newer === undefined ? patch : this.options.merge(patch, newer))
        this.failed = true
        this.options.status('error', error instanceof Error ? error.message : '保存失败，请重试')
        return
      }
    }
    this.failed = false
    this.options.status('saved')
  }
}
