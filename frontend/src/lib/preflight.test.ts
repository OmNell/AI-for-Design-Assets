import { describe, expect, it } from 'vitest'
import { preflight, rules, type Context, type ManifestEntry } from './preflight'
const file = (rel_path: string): ManifestEntry => ({ rel_path, size: 1, mtime: 1, is_dir: false })
const dir = (rel_path: string): ManifestEntry => ({ rel_path, size: 0, mtime: 0, is_dir: true })
const valid: Context = { folder: 'P001_企业_项目', entries: [file('单品/主图.jpg'), ...['01定稿文件', '02设计方案', '03拍摄照片及视频', '04授权文件', '05 详情页'].map(dir)] }
const cases: Record<string, [Partial<Context>, Partial<Context>]> = {
  P01: [{ folder: '两段_项目' }, {}], P02: [{ exists: true }, { exists: false }],
  P03: [{ entries: [file('资料/a:b.txt')] }, {}], P04: [{ entries: [file('资料/' + 'a'.repeat(200))] }, {}],
  P05: [{ entries: [dir('资料/套装档案')] }, {}], P06: [{ entries: [dir('单品/嵌套')] }, {}],
  P07: [{ entries: [dir('单品'), file('单品/Thumbs.db')] }, {}], P08: [{ entries: [file('单品/a.png')] }, {}],
  P09: [{ folder: 'P001_企业_项目 名·' }, {}], P10: [{ entries: [file('根目录.xlsx')] }, { entries: [file('单品/a.png')] }],
  P11: [{ entries: [file('单品/随便命名.jpg')] }, { entries: [dir('资料')] }],
  P12: [{ entries: [file('资料/Thumbs.db'), file('单品/~$临时.png')] }, {}],
  P13: [{ entries: [dir('空目录')] }, { entries: [dir('目录'), file('目录/a.txt')] }],
}
describe('命名规范第 11 节全部规则，每条含命中与不命中', () => {
  for (const rule of rules) it(rule.code + ' ' + rule.message, () => {
    const [hit, miss] = cases[rule.code]
    expect(rule.detect({ ...valid, ...hit }).length).toBeGreaterThan(0)
    expect(rule.detect({ ...valid, ...miss })).toEqual([])
  })
})
it('垃圾文件先忽略，不触发主图类型拦截；空目录、资料内容不丢弃', () => {
  const issues = preflight({ ...valid, entries: [...valid.entries, file('单品/desktop.ini'), file('根目录.xlsx'), dir('任意目录')] })
  expect(issues.filter(i => i.level === 'block')).toEqual([])
  expect(issues.filter(i => i.level === 'ignore').map(i => i.path)).toEqual(['单品/desktop.ini'])
})
it('jpg/jpeg/png 大小写接受，其他图片扩展名仍拦截', () => {
  for (const extension of ['jpg', 'jpeg', 'png', 'JPG']) expect(preflight({ ...valid, entries: [file('单品/a.' + extension)] }).filter(i => i.rule_code === 'P06')).toEqual([])
  expect(preflight({ ...valid, entries: [file('单品/a.webp')] }).some(i => i.rule_code === 'P06')).toBe(true)
})
