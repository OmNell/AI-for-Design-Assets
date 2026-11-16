// @vitest-environment jsdom
import { act, useEffect } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { GlassProvider, useGlass } from './GlassProvider'
import { GlassSurface } from './GlassSurface'
import { MODE_KEY } from './visualMode'

let host: HTMLDivElement
let root: Root
let resize: () => void
const disconnect = vi.fn()

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('CSS', { supports: () => true })
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Chrome/140 Safari/537.36')
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
    observe() {}
    disconnect() { disconnect() }
  })
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

function Toggle() {
  const { toggle } = useGlass()
  return <button onClick={toggle}>切换</button>
}

function renderSurfaces() {
  act(() => root.render(<GlassProvider><Toggle />
    <GlassSurface><input aria-label="保留输入" defaultValue="内容" /></GlassSurface>
    <GlassSurface priority="secondary">次要条目</GlassSurface>
  </GlassProvider>))
}

function clickToggle() {
  act(() => host.querySelector('button')!.click())
}

describe('React 18 下的真实玻璃库集成', () => {
  it('完整模式挂载真实 SVG 位移滤镜并绑定背景层，滤镜 ID 唯一', () => {
    renderSurfaces()
    const filters = [...host.querySelectorAll('filter')]
    expect(filters).toHaveLength(2)
    expect(new Set(filters.map(filter => filter.id)).size).toBe(2)
    expect(host.querySelectorAll('feDisplacementMap')).toHaveLength(6)
    host.querySelectorAll<HTMLElement>('.glass__warp').forEach((warp, i) => {
      expect(warp.style.filter).toBe(`url(#${filters[i].id})`)
      expect(warp.style.backdropFilter).toContain('blur(')
    })
    expect(host.querySelectorAll('[data-glass-renderer="svg"]')).toHaveLength(2)
  })

  it('轻量模式保留主要容器的滤镜，卸载次要滤镜，保存偏好且保持内容节点', () => {
    renderSurfaces()
    const input = host.querySelector('input')!
    input.value = '未提交内容'
    clickToggle()
    expect(host.querySelectorAll('filter')).toHaveLength(1)
    expect(host.querySelectorAll('[data-glass-renderer="none"]')).toHaveLength(1)
    expect(localStorage.getItem(MODE_KEY)).toBe('lite')
    expect(host.querySelector('input')).toBe(input)
    expect(input.value).toBe('未提交内容')
    clickToggle()
    expect(host.querySelectorAll('filter')).toHaveLength(2)
    expect(host.querySelector('input')).toBe(input)
    expect(localStorage.getItem(MODE_KEY)).toBe('full')
  })

  it('尺寸变化刷新装饰滤镜，不重挂载正文，卸载时清理监听器', () => {
    const mounted = vi.fn()
    function Content() { useEffect(() => { mounted() }, []); return <input /> }
    act(() => root.render(<GlassProvider><GlassSurface><Content /></GlassSurface></GlassProvider>))
    const input = host.querySelector('input')
    const filter = host.querySelector('filter')
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({ width: 600, height: 250 } as DOMRect)
    act(() => resize())
    expect(host.querySelector('filter')).not.toBe(filter)
    expect(host.querySelector('input')).toBe(input)
    expect(mounted).toHaveBeenCalledTimes(1)
  })

  it('鼠标移动驱动实际库的高光重绘', () => {
    renderSurfaces()
    const surface = host.querySelector<HTMLElement>('.glass-surface')!
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 600, height: 250 } as DOMRect)
    const effect = host.querySelector('.glass-effect')!
    const before = effect.innerHTML
    act(() => surface.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 500, clientY: 180 })))
    expect(effect.innerHTML).not.toBe(before)
  })

  it.each(['Firefox/140', 'Version/18 Safari/605'])('%s 使用 CSS 玻璃且默认轻量', agent => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(agent)
    renderSurfaces()
    expect(host.querySelector('[data-visual-mode="lite"]')).not.toBeNull()
    expect(host.querySelector('[data-glass-renderer="css"]')).not.toBeNull()
    expect(host.querySelector('filter')).toBeNull()
    clickToggle()
    expect(host.querySelectorAll('[data-glass-renderer="css"]')).toHaveLength(2)
  })

  it('缺少背景滤镜支持时保持正文并使用 CSS 回退', () => {
    vi.stubGlobal('CSS', { supports: () => false })
    renderSurfaces()
    expect(host.querySelector('filter')).toBeNull()
    expect(host.querySelector('input')).not.toBeNull()
  })

  it('刷新后读取保存的轻量偏好', () => {
    localStorage.setItem(MODE_KEY, 'lite')
    renderSurfaces()
    expect(host.querySelectorAll('filter')).toHaveLength(1)
  })
})
