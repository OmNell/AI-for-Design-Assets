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
