import { Link } from 'react-router'
import { operationLabels, type FrozenOperation, type SnapshotValue } from '../../api/operations'
import { formatBytes } from '../../lib/terms'
import { GlassPanel } from '../glass/GlassPanel'
import { PurgeList } from './OperationExecution'

export const fieldLabels: Record<string, string> = { category: '品类', material: '材质', color: '主色', style: '风格', festival: '节日场景',
  audience: '受众群体', usage_scene: '使用场景', size: '尺寸', craft: '工艺', material_name: '物料名称', structure: '结构' }
const statusLabels: Record<string, string> = { active: '有效', voided: '已作废', deleted: '已删除', replaced: '历史版本', inactive: '已失效' }
const captions: Record<string, string> = { project_name: '项目名称', folder_name: '项目文件夹', company_name: '企业名称',
  company_id: '企业编号', industry_id: '行业编号', won: '中标状态', status: '状态', canonical_path: '存放目录', current_path: '存放目录',
  display_name: '商品名称', kind: '类型', free_text: '自由描述', rel_path: '相对路径', physical_rel_path: '文件实际位置',
  fingerprint: 'SHA-256', size: '大小', mtime: '修改时间', value: '标签值', source: '来源', confirmation_status: '确认状态' }
const values: Record<string, string> = { ...statusLabels, item: '单品', set: '套装', human: '人工确认', manual: '人工修改', ai: 'AI',
  confirmed_unknown: '已确认无法判断', confirmed_value: '已确认具体值', pending: '未确认' }

function show(value: SnapshotValue | undefined, other: SnapshotValue | undefined, fieldsOnly: boolean) {
  if (value === null || value === undefined) return '—'
  if (typeof value !== 'object') return values[String(value)] ?? String(value)
  const keys = Object.keys(value).filter(key => !fieldsOnly || typeof other !== 'object' || other === null || value[key] !== other[key])
  return <dl className="space-y-1">{keys.map(key => {
    const entry = value[key]
    const shown = key === 'won' ? entry ? '已中标' : '未中标'
      : key === 'size' && typeof entry === 'number' ? formatBytes(entry)
        : key === 'mtime' && typeof entry === 'number' ? new Date(entry).toLocaleString('zh-CN')
          : key === 'value' ? String(entry || '（空）') : entry !== null ? values[String(entry)] ?? String(entry) : '—'
    return <div key={key}><dt className="inline text-xs text-slate-500">{captions[key] ?? key}　</dt><dd className="inline">{shown}</dd></div>
  })}</dl>
}
function changeTitle(group: string, id: string): string {
  const [code, field] = id.split(':')
  if (group === 'project') return '项目'
  if (group === 'tags') return `${code} · ${fieldLabels[field] ?? field}`
  if (group === 'relations') return `套装关联 ${code} → ${field}`
  if (group === 'products') return `商品 ${id}`
  return /^\d+$/.test(id) ? `文件 #${id}` : `新增文件 ${id}`
}

export function OperationDiff({ operation }: { operation: FrozenOperation }) {
  const { before, after, purge, params } = operation
  const rows = Object.entries(after ?? {}).flatMap(([group, changes]) => Object.entries(changes ?? {}).map(([id, change]) => ({ group, id, ...change })))
  const codes = new Map(before.products.map(row => [row.id, row.product_code]))
  return <GlassPanel><div className="space-y-4">
    <div><h2 className="text-xl font-semibold">变更前后对比 · {operationLabels[operation.kind]}</h2>
      {params.reason && <p className="mt-2 whitespace-pre-wrap break-all text-sm text-slate-700">操作原因　{params.reason}</p>}</div>
    {!!rows.length && <div className="max-h-[30rem] overflow-auto"><table className="w-full table-fixed text-left text-sm">
      <thead className="sticky top-0 bg-white/95"><tr><th className="w-1/5 p-2">变更项</th><th className="w-2/5 p-2">当前版本</th><th className="w-2/5 p-2">拟发布版本</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index} className="border-t border-slate-400/20 align-top">
        <th scope="row" className="break-words p-2 font-medium">{changeTitle(row.group, row.id)}</th>
        <td className="whitespace-pre-wrap break-all p-2">{show(row.before, row.after, row.group === 'project')}{row.group === 'files' && row.before && /^\d+$/.test(row.id) && <Link to={`/files/${row.id}`} className="mt-1 block text-xs text-emerald-800 underline">查看原文件</Link>}</td>
        <td className="whitespace-pre-wrap break-all p-2">{show(row.after, row.before, row.group === 'project')}</td>
      </tr>)}</tbody>
    </table></div>}
    {purge && <PurgeList preview={{ purge_files: purge.files.map(file => ({ ...file, history: file.historical ?? purge.history.some(row => row.file_id === file.file_id) })),
      product_codes: [purge.project_code, ...purge.products.map(row => row.product_code)], relationships: purge.relations.map(row =>
        `${codes.get(row.set_product_id) ?? row.set_product_id} → ${codes.get(row.item_product_id) ?? row.item_product_id}`) }} />}
    {!rows.length && !purge && <p className="text-sm text-slate-500">暂无可显示的变更。</p>}
  </div></GlassPanel>
}
