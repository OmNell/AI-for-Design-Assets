import type { LocalEntry } from './dirReader'
export type Level = 'block' | 'warn' | 'ignore'
export interface Issue { rule_code: string; level: Level; path: string; message: string }
export interface ManifestEntry { rel_path: string; size: number; mtime: number; is_dir: boolean }
export interface Context { folder: string; entries: ManifestEntry[]; exists?: boolean; prefix_length?: number }
interface Rule { code: string; level: Level | null; message: string; detect: (context: Context) => string[] }
export const requiredDirectories = ['01定稿文件', '02设计方案', '03拍摄照片及视频', '04授权文件', '05 详情页']
export function isIgnored(entry: { rel_path: string; is_dir: boolean }): boolean {
  const name = entry.rel_path.split('/').at(-1)!
  return !entry.is_dir && (/^(thumbs\.db|\.ds_store|desktop\.ini)$/i.test(name) || name.startsWith('~$'))
}
const active = (context: Context) => context.entries.filter(entry => !isIgnored(entry))
const unsafe = (path: string) => path.split('/').some(part => !part || part === '.' || part === '..' || /[\\:*?"<>|\u0000-\u001f]/.test(part) || /[. ]$/.test(part) || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$/i.test(part))
export const rules: Rule[] = [
  { code: 'P01', level: 'block', message: '项目文件夹名必须为 编号_企业_项目名 三段', detect: c => c.folder.split('_').length !== 3 || c.folder.split('_').some(p => !p) ? [c.folder] : [] },
  // 编号已入库时按补充处理；能否补充（项目有效、没有未结束的操作）由服务端判定。
  { code: 'P02', level: null, message: '该项目已入库，本次上传按补充处理', detect: c => c.exists ? [c.folder] : [] },
  { code: 'P03', level: 'block', message: '名称含非法字符或不安全的路径', detect: c => [c.folder, ...active(c).map(e => e.rel_path)].filter((p, i) => unsafe(p) || (i === 0 && p.includes('/'))) },
  { code: 'P04', level: 'block', message: '项目内完整路径超过 200 字符', detect: c => [...(c.folder.length + (c.prefix_length ?? 0) > 200 ? [c.folder] : []), ...active(c).filter(e => `${c.folder}/${e.rel_path}`.length + (c.prefix_length ?? 0) > 200).map(e => e.rel_path)] },
  { code: 'P05', level: 'block', message: '出现系统保留名称', detect: c => [c.folder, ...active(c).map(e => e.rel_path)].filter(p => p.split('/').some(name => ['套装档案', '单品档案', '_历史版本', '_项目档案.txt'].includes(name.toLowerCase()))) },
  { code: 'P06', level: 'block', message: '套装/单品中只能平铺 jpg、jpeg、png 图片', detect: c => active(c).filter(e => {
    const parts = e.rel_path.split('/'); return ['套装', '单品'].includes(parts[0]) && (e.is_dir ? parts.length > 1 : parts.length !== 2 || !/\.(jpe?g|png)$/i.test(parts[1]))
  }).map(e => e.rel_path) },
  { code: 'P07', level: 'warn', message: '套装和单品均为空', detect: c => c.exists || active(c).some(e => !e.is_dir && /^(套装|单品)\/[^/]+\.(jpe?g|png)$/i.test(e.rel_path)) ? [] : ['套装/单品'] },
  { code: 'P08', level: 'warn', message: '缺少资料夹', detect: c => c.exists ? [] : requiredDirectories.filter(name => !active(c).some(e => e.rel_path === name && e.is_dir || e.rel_path.startsWith(name + '/'))) },
  { code: 'P09', level: 'warn', message: '项目名含不建议的空格或标点', detect: c => c.folder.split('_').length === 3 && /[·—\s\uFF00-\uFFEF\u3000-\u303F]/.test(c.folder.split('_')[2]) ? [c.folder] : [] },
  { code: 'P10', level: null, message: '根散文件、其他目录和资料夹内容不作业务检查', detect: c => active(c).filter(e => !['套装', '单品'].includes(e.rel_path.split('/')[0])).map(e => e.rel_path) },
  { code: 'P11', level: null, message: '不检查分辨率、主图命名和同名', detect: c => active(c).filter(e => !e.is_dir && /^(套装|单品)\/[^/]+\.(jpe?g|png)$/i.test(e.rel_path)).map(e => e.rel_path) },
  { code: 'P12', level: 'ignore', message: '系统垃圾文件不上传', detect: c => c.entries.filter(isIgnored).map(e => e.rel_path) },
  { code: 'P13', level: null, message: '空文件夹照常上传', detect: c => active(c).filter(e => e.is_dir && !active(c).some(child => child.rel_path.startsWith(e.rel_path + '/'))).map(e => e.rel_path) },
]
export const manifest = (entries: LocalEntry[]): ManifestEntry[] => entries.map(e => ({ rel_path: e.rel_path, size: e.file?.size ?? 0, mtime: e.file?.lastModified ?? 0, is_dir: e.is_dir }))
export function preflight(context: Context): Issue[] {
  return rules.flatMap(rule => rule.level ? rule.detect(context).map(path => ({ rule_code: rule.code, level: rule.level!, path, message: rule.message })) : [])
}
