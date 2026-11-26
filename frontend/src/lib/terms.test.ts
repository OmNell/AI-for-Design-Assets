import { expect, it } from 'vitest'
import { formatBytes, lastSegment, removeTerm, replaceLastSegment, splitTerms } from './terms'

it('按后端相同的分隔符拆分并去重', () => {
  expect(splitTerms(' 陶瓷、胡桃木，竹,钢；铁;铜/银  金 陶瓷 ')).toEqual(['陶瓷', '胡桃木', '竹', '钢', '铁', '铜', '银', '金'])
  expect(splitTerms('  ')).toEqual([])
})
it('删除一个小标签后其余值用顿号连接', () => {
  expect(removeTerm('陶瓷，胡桃木 / 竹', '胡桃木')).toBe('陶瓷、竹')
  expect(removeTerm('陶瓷', '陶瓷')).toBe('')
})
it('输入提示只取最后一段并替换最后一段', () => {
  expect(lastSegment('陶瓷、胡')).toBe('胡')
  expect(lastSegment('陶瓷、')).toBe('')
  expect(replaceLastSegment('陶瓷、胡', '胡桃木')).toBe('陶瓷、胡桃木')
  expect(replaceLastSegment('胡', '胡桃木')).toBe('胡桃木')
})
it('文件大小使用易读单位', () => {
  expect(formatBytes(512)).toBe('512 B')
  expect(formatBytes(1536)).toBe('1.5 KB')
  expect(formatBytes(2147483648)).toBe('2.0 GB')
})
