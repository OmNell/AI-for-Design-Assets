import { expect, it } from 'vitest'
import { readDrop, readInput } from './dirReader'

it('webkitdirectory 清单统一为相对路径并保留可推导父目录', () => {
  const file = new File(['a'], 'a.txt')
  Object.defineProperty(file, 'webkitRelativePath', { value: '项目/中文目录/a.txt' })
  const result = readInput([file])
  expect(result.folder_name).toBe('项目')
  expect(result.entries.map(e => e.rel_path)).toEqual(['中文目录', '中文目录/a.txt'])
  expect(result.preservesEmptyDirectories).toBe(false)
})
it('多顶层文件夹被拦截', () => {
  const values = ['A', 'B'].map(name => { const f = new File(['x'], 'x.txt'); Object.defineProperty(f, 'webkitRelativePath', { value: name + '/x.txt' }); return f })
  expect(() => readInput(values)).toThrow('一个项目文件夹')
})
it('拖入递归读取所有批次，保留空目录，拦截多个根', async () => {
  let calls = 0
  const empty = { name: '空目录', isDirectory: true, isFile: false, createReader: () => ({ readEntries: (done: (values: unknown[]) => void) => done([]) }) }
  const file = { name: 'a.txt', isDirectory: false, isFile: true, file: (done: (file: File) => void) => done(new File(['a'], 'a.txt')) }
  const root = { name: '项目', isDirectory: true, isFile: false, createReader: () => ({ readEntries: (done: (values: unknown[]) => void) => done(++calls === 1 ? [empty] : calls === 2 ? [file] : []) }) }
  const items = [{ kind: 'file', webkitGetAsEntry: () => root }] as unknown as DataTransferItemList
  const result = await readDrop(items)
  expect(result.entries.map(e => e.rel_path)).toEqual(['空目录', 'a.txt'])
  expect(calls).toBe(3)
  await expect(readDrop([...Array.from(items), ...Array.from(items)] as unknown as DataTransferItemList)).rejects.toThrow('一个项目文件夹')
})
