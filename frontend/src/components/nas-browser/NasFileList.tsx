import { nasPreviewUrl, type NasEntry } from '../../api/nasBrowser'
import { formatBytes } from '../../lib/terms'
import { FileIcon, FileThumbnail, Table } from '../ui'

export const nasType = (item: NasEntry) => item.is_dir ? '文件夹' : item.extension.toUpperCase() || '文件'
export const nasTime = (value: string) => new Date(value).toLocaleString('zh-CN')

interface Props {
  entries: NasEntry[]
  selected: string | undefined
  view: 'list' | 'grid'
  onSelect: (path: string) => void
  onOpen: (path: string) => void
}

export function NasFileList({ entries, selected, view, onSelect, onOpen }: Props) {
  const choose = (item: NasEntry) => onSelect(item.path)
  const open = (item: NasEntry) => { if (item.is_dir) onOpen(item.path) }
  if (view === 'grid') return <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,9rem),1fr))] gap-3 p-3">
    {entries.map(item => <button type="button" key={item.path}
      className={'min-w-0 rounded-lg border p-2 text-left ' + (selected === item.path ? 'border-blue-300 bg-blue-50' : 'border-slate-200')}
      aria-pressed={selected === item.path} onClick={() => choose(item)} onDoubleClick={() => open(item)}>
      {item.is_dir ? <div className="grid aspect-[4/3] place-items-center rounded-md bg-blue-50"><FileIcon folder /></div> :
        <FileThumbnail name={item.name} src={item.kind === 'image' ? nasPreviewUrl(item.path, 300) : undefined} />}
      <span className="mt-2 block break-all font-medium">{item.name}</span>
      <span className="text-xs text-slate-500">{item.is_dir ? '文件夹' : formatBytes(item.size)}</span>
    </button>)}
  </div>
  return <Table label="NAS 文件列表">
    <thead><tr><th>名称</th><th>类型</th><th>大小</th><th>修改时间</th></tr></thead>
    <tbody>{entries.map(item => <tr key={item.path} className={selected === item.path ? 'bg-blue-50' : ''}
      onClick={() => choose(item)} onDoubleClick={() => open(item)}>
      <td><button type="button" className="flex items-center gap-2 text-left" aria-pressed={selected === item.path}
        onKeyDown={event => { if (event.key === 'Enter' && item.is_dir) { event.preventDefault(); open(item) } }}>
        <FileIcon folder={item.is_dir} name={item.name} /><span className="break-all">{item.name}</span></button></td>
      <td>{nasType(item)}</td><td className="whitespace-nowrap">{item.is_dir ? '—' : formatBytes(item.size)}</td>
      <td className="whitespace-nowrap">{nasTime(item.modified_at)}</td>
    </tr>)}</tbody>
  </Table>
}
