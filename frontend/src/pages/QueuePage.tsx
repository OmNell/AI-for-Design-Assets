import { Panel, Table } from '../components/ui'
import { Link } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { getQueue, type Stage } from '../api/audit'
import { operationLabels } from '../api/operations'
import { formatBytes } from '../lib/terms'

export function formatWaiting(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))} 分钟`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时`
  return `${Math.floor(seconds / 86400)} 天 ${Math.floor(seconds % 86400 / 3600)} 小时`
}

export function QueuePage({ stage }: { stage: Stage }) {
  const queue = useQuery({ queryKey: ['queue', stage], queryFn: () => getQueue(stage), retry: false, refetchInterval: 30000 })
  const title = stage === 'first' ? '待初审' : '待终审'
  return <Panel><h1 className="text-2xl font-semibold">{title}</h1>
    <p className="mb-6 mt-3 text-sm text-slate-500">按提交时间排列。两级审核查看同一份提交内容。具备对应审核权限的账号可以审核自己提交的批次。同一轮的初审与终审由不同账号完成。</p>
    {queue.isPending ? <p>正在加载队列…</p> : queue.isError ? <p role="alert" className="text-red-800">{queue.error.message}</p> : !queue.data.length ? <p className="text-slate-500">队列是空的。</p> :
      <Table><thead className="text-xs text-slate-500"><tr><th className="p-3">项目</th><th className="p-3">上传人</th><th className="p-3">商品数</th><th className="p-3">文件量</th><th className="p-3">等待时长</th><th className="p-3">轮次</th><th className="p-3">领取</th><th className="p-3" /></tr></thead>
        <tbody>{queue.data.map(item => <tr key={item.id} className="border-t border-slate-500/10">
          <td className="p-3"><p className="break-all font-medium">{item.folder_name}</p><p className="mt-1 text-xs text-slate-500">{item.batch_no}{item.kind && item.kind !== 'ingest' && ` · ${operationLabels[item.kind]}`}</p></td>
          <td className="p-3">{item.uploader_name}</td><td className="p-3">{item.products}</td><td className="p-3">{item.files} 个 · {formatBytes(item.total_size)}</td>
          <td className="p-3">{formatWaiting(item.waiting_seconds)}</td><td className="p-3">第 {item.round} 轮</td>
          <td className="p-3">
            {item.blocked ? <span className="text-amber-900">本轮初审人不能终审</span> : item.claimed_by_me ? '我已领取' : item.claimed_by ? `${item.claimed_by.name} 已领取` : '未领取'}
          </td>
          <td className="p-3"><Link to={`/review/${item.id}`} className="text-blue-700 underline underline-offset-4">{item.blocked || (item.claimed_by && !item.claimed_by_me) ? '查看' : '进入审核'}</Link></td></tr>)}</tbody></Table>}
  </Panel>
}
