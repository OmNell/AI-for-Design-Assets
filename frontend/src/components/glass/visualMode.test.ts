import { expect, it } from 'vitest'
import { initialMode } from './visualMode'

it('优先使用已保存的视觉模式', () => {
  expect(initialMode('full', 'Safari')).toBe('full')
  expect(initialMode('lite', 'Chrome Safari')).toBe('lite')
})
it('Safari 和 Firefox 默认轻量，Chrome 和 Edge 默认完整', () => {
  expect(initialMode(null, 'Version/18 Safari')).toBe('lite')
  expect(initialMode(null, 'Firefox/140')).toBe('lite')
  expect(initialMode(null, 'Chrome/140 Safari')).toBe('full')
  expect(initialMode('invalid', 'Chrome/140 Edg/140 Safari')).toBe('full')
})
