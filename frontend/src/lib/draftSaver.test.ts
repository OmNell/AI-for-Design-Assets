import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { DraftSaver, type SaveStatus } from './draftSaver'

type Patch = Record<string, string>
let statuses: SaveStatus[]
let sent: [number, Patch][]
let saved: [number, string, Patch | undefined][]
const create = (send: (id: number, patch: Patch) => Promise<string>) => new DraftSaver<Patch, string>({ delay: 600,
  merge: (older, newer) => ({ ...older, ...newer }), send: (id, patch) => { sent.push([id, patch]); return send(id, patch) },
  saved: (id, result, newer) => saved.push([id, result, newer]), status: status => statuses.push(status) })
beforeEach(() => { vi.useFakeTimers(); statuses = []; sent = []; saved = [] })
afterEach(() => vi.useRealTimers())

it('防抖期间的修改合并成一次请求', async () => {
  const saver = create(async () => 'ok')
  saver.change(1, { a: '1' }); saver.change(1, { a: '2', b: '3' }); saver.change(2, { c: '4' })
  expect(sent).toEqual([])
  expect(saver.idle).toBe(false)
  await vi.advanceTimersByTimeAsync(600)
  expect(sent).toEqual([[1, { a: '2', b: '3' }], [2, { c: '4' }]])
  expect(statuses.at(-1)).toBe('saved')
  expect(saver.idle).toBe(true)
})
it('保存期间的新修改交给回调并随后再次发送', async () => {
  let release: (value: string) => void = () => undefined
  const saver = create(() => new Promise<string>(resolve => { release = resolve }))
  saver.change(1, { a: '1' })
  const flushing = saver.flush()
  saver.change(1, { a: '2' })
  expect(saver.pending(1)).toEqual({ a: '2' })
  release('first')
  await vi.advanceTimersByTimeAsync(0)
  expect(saved[0]).toEqual([1, 'first', { a: '2' }])
  release('second')
  await flushing
  await vi.advanceTimersByTimeAsync(600)
  expect(sent).toEqual([[1, { a: '1' }], [1, { a: '2' }]])
  expect(statuses.at(-1)).toBe('saved')
})
it('失败的修改保留在队列里，重试成功后恢复为已保存', async () => {
  let fail = true
  const saver = create(async () => { if (fail) throw new Error('网络错误'); return 'ok' })
  saver.change(1, { a: '1' })
  expect(await saver.flush()).toBe(false)
  expect(statuses.at(-1)).toBe('error')
  expect(saver.pending(1)).toEqual({ a: '1' })
  saver.change(1, { b: '2' })
  fail = false
  expect(await saver.flush()).toBe(true)
  expect(sent.at(-1)).toEqual([1, { a: '1', b: '2' }])
  expect(statuses.at(-1)).toBe('saved')
})
it('离开页面时取走未发送的草稿', () => {
  const saver = create(async () => 'ok')
  saver.change(3, { a: '1' })
  expect(saver.drain()).toEqual([[3, { a: '1' }]])
  expect(saver.pending(3)).toBeUndefined()
})
