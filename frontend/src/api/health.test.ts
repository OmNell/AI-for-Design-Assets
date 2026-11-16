import { afterEach, describe, expect, it, vi } from 'vitest'
import { getHealth } from './health'

afterEach(() => vi.unstubAllGlobals())
describe('健康接口', () => {
  it('读取并校验后端状态', async () => {
    const data = { version: '0.1.0', mode: 'dev', database_available: true }
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(data)))
    vi.stubGlobal('fetch', fetch)
    expect(await getHealth()).toEqual(data)
    expect(fetch).toHaveBeenCalledWith('/api/health', { signal: undefined })
  })
  it('将失败状态转为中文提示', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 503 })))
    await expect(getHealth()).rejects.toThrow('暂时无法获取运行状态')
  })
  it('拒绝未知状态结构', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"mode":"other"}')))
    await expect(getHealth()).rejects.toThrow('格式不正确')
  })
})
