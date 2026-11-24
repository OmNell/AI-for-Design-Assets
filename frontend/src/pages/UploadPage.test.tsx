// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { UploadPage } from './UploadPage'

const project = 'P2026-0001_企业_项目'
const precheck = { project_code: 'P2026-0001', company_name: '企业', project_name: '项目', code_status: 'new', issues: [] }
let host: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  HTMLDialogElement.prototype.showModal ??= function () { this.open = true }
  HTMLDialogElement.prototype.close ??= function () { this.open = false }
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function button(text: string): HTMLButtonElement | undefined {
  return [...host.querySelectorAll('button')].find(element => element.textContent?.trim() === text)
}

async function draw() {
  act(() => root.render(<MemoryRouter><UploadPage /></MemoryRouter>))
  await act(async () => host.querySelector<HTMLInputElement>('input[type="radio"]')!.click())
}

it('文件夹经浏览器授权后立即显示读取状态，预检前保留空目录', async () => {
  let supplyFile!: (value: File) => void
  let supplyPrecheck!: (value: Response) => void
  const preview = vi.fn(() => new Promise<Response>(resolve => { supplyPrecheck = resolve }))
  vi.stubGlobal('fetch', preview)
  const picker = vi.fn(function (this: Window, mode) {
    expect(this).toBe(window)
    expect(mode).toEqual({ mode: 'read' })
    return Promise.resolve({
      name: project, kind: 'directory',
      async *values() {
        yield { kind: 'directory', name: '空目录', async *values() { /* 空目录也进入清单。 */ } }
        yield { kind: 'file', name: '说明.txt', getFile: () => new Promise<File>(resolve => { supplyFile = resolve }) }
      },
    })
  })
  vi.stubGlobal('showDirectoryPicker', picker)
  await draw()
  await act(async () => button('选择文件夹')!.click())
  expect(host.textContent).toContain('正在读取文件夹')
  expect(button('选择文件夹')?.disabled).toBe(true)
  expect(preview).not.toHaveBeenCalled()
  await act(async () => supplyFile(new File(['文本'], '说明.txt')))
  expect(host.textContent).toContain('正在预检文件夹')
  expect(preview).toHaveBeenCalledOnce()
  const [url, options] = preview.mock.calls[0] as unknown as [string, RequestInit]
  expect(url).toBe('/api/batches/precheck')
  expect(JSON.parse(String(options.body)).files).toEqual(expect.arrayContaining([
    expect.objectContaining({ rel_path: '空目录', is_dir: true }),
    expect.objectContaining({ rel_path: '说明.txt', is_dir: false }),
  ]))
  await act(async () => supplyPrecheck(Response.json(precheck)))
  expect(host.textContent).toContain('预检结果')
  expect(host.textContent).not.toContain('正在预检文件夹')
})

async function openUploadConfirmation(won = true) {
  vi.stubGlobal('showDirectoryPicker', vi.fn(async () => ({
    kind: 'directory',
    name: project,
    async *values() {
      yield {
        kind: 'directory', name: '单品',
        async *values() {
          yield { kind: 'file', name: '主图.jpg', getFile: async () => new File(['图片'], '主图.jpg') }
        },
      }
    },
  })))

  await draw()
  if (!won) await act(async () => host.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1].click())
  await act(async () => button('选择文件夹')!.click())
  await act(async () => host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]').forEach(input => input.click()))
  await act(async () => button('开始上传')!.click())
}

it.each(['AbortError', 'NotAllowedError', 'SecurityError'])('选择器返回 %s 时显示提示，可用普通选择继续', async (name) => {
  vi.stubGlobal('showDirectoryPicker', vi.fn().mockRejectedValue(new DOMException('不可读取', name)))
  const fetchMock = vi.fn(async () => Response.json(precheck))
  vi.stubGlobal('fetch', fetchMock)
  await draw()
  await act(async () => button('选择文件夹')!.click())
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('浏览器未能读取这个文件夹')
  expect(fetchMock).not.toHaveBeenCalled()
  const input = host.querySelector<HTMLInputElement>('input[webkitdirectory]')!
  const file = new File(['内容'], '资料.txt')
  Object.defineProperty(file, 'webkitRelativePath', { value: project + '/资料.txt' })
  const native = vi.spyOn(input, 'click').mockImplementation(() => {
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await act(async () => button('使用普通选择')!.click())
  expect(native).toHaveBeenCalledOnce()
  expect(fetchMock).toHaveBeenCalledOnce()
  expect(host.textContent).toContain('预检结果')
  expect(host.textContent).toContain('无法列出空目录')
})

it('普通选择完成但没收到文件时显示提示并允许重选', async () => {
  vi.stubGlobal('showDirectoryPicker', undefined)
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  await draw()
  const input = host.querySelector<HTMLInputElement>('input[webkitdirectory]')!
  vi.spyOn(input, 'click').mockImplementation(() => {
    input.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await act(async () => button('选择文件夹')!.click())
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('空目录请使用拖入方式')
  expect(fetchMock).not.toHaveBeenCalled()
  expect(button('选择文件夹')?.disabled).toBe(false)
})

it('普通选择窗口关闭且未返回文件时清除等待状态', async () => {
  vi.stubGlobal('showDirectoryPicker', undefined)
  await draw()
  const input = host.querySelector<HTMLInputElement>('input[webkitdirectory]')!
  vi.spyOn(input, 'click').mockImplementation(() => undefined)
  await act(async () => button('选择文件夹')!.click())
  expect(host.querySelector('[role="status"]')?.textContent).toContain('请在浏览器对话框中确认')
  await act(async () => input.dispatchEvent(new Event('cancel', { bubbles: true })))
  expect(host.querySelector('[role="status"]')).toBeNull()
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('可以重新选择或直接拖入')
})

it.each([
  { won: true, path: '01已中标项目/02酒业/07企业' },
  { won: false, path: '02未中标项目/02酒业/07企业' },
  { won: true, path: null },
])('确认上传显示与中标状态对应的归档建议（$path）', async ({ won, path }) => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({
    ...precheck,
    won_path: path ? '01已中标项目/02酒业/07企业' : null,
    lost_path: path ? '02未中标项目/02酒业/07企业' : null,
  })))
  await openUploadConfirmation(won)
  expect(document.querySelector('dialog[open]')?.textContent).toContain(
    (won ? '已中标' : '未中标') + ' · ' + (path ?? '位置待定'),
  )
})

it('新项目按上一次完成的实际传输速度预估耗时', async () => {
  const file = { id: 3, rel_path: '单品/主图.jpg', size: 6, bytes_received: 0, upload_status: 'pending',
    thumb_status: null, thumb_error: null }
  const batch = { id: 2, batch_no: 'B02', project_code: 'P2026-0001', folder_name: project,
    status: '上传中', queued: false, upload_token: 'token', chunk_bytes: 8, parallel_files: 1, files: [file] }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path === '/api/batches/precheck') return Response.json(precheck)
    if (path === '/api/batches') return Response.json(batch)
    if (path === '/api/batches/2/files/3/chunks/0') return Response.json({ bytes_received: 6 })
    if (path === '/api/batches/2/files/3/complete') return Response.json({
      ...batch, status: '已暂存', upload_token: null, files: [{ ...file, bytes_received: 6, upload_status: 'done' }],
    })
    throw new Error('未预期的接口请求')
  }))

  await openUploadConfirmation()
  expect(document.querySelector('dialog[open]')?.textContent).toContain('开始传输后将按实际速度估算剩余时间')
  await act(async () => document.querySelector<HTMLButtonElement>('dialog[open] button')!.click())
  expect(host.textContent).toContain('已暂存')
  act(() => root.unmount())
  root = createRoot(host)

  await openUploadConfirmation()
  expect(document.querySelector('dialog[open]')?.textContent).toMatch(/按上次传输速度预计约 \d+ 秒/)
})
