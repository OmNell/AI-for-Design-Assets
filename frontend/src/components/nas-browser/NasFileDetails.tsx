import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getNasInfo, getNasPreview, nasDownloadUrl } from '../../api/nasBrowser'
import { formatBytes } from '../../lib/terms'
import { Badge, Button, ContextPane, ErrorState, LoadingState } from '../ui'
import { nasTime, nasType } from './NasFileList'

function ImagePreview({ path, name }: { path: string; name: string }) {
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    void getNasPreview(path, controller.signal).then(blob => {
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(blob)
      setUrl(objectUrl)
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '无法加载预览')
    })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [path])
  if (error) return <ErrorState>{error}</ErrorState>
  return url ? <img className="max-h-64 w-full rounded-md bg-slate-50 object-contain" alt={name + '预览'} src={url} />
    : <LoadingState>正在加载预览…</LoadingState>
}

export function NasFileDetails({ root, path, onClose, onOpen }: { root: string; path: string; onClose: () => void; onOpen: (path: string) => void }) {
  const info = useQuery({ queryKey: ['nas-browser', path, 'info'], queryFn: ({ signal }) => getNasInfo(path, signal) })
  const [copyStatus, setCopyStatus] = useState('')
  const item = info.data
  const displayPath = root + '/' + path
  const copy = async () => {
    try { await navigator.clipboard.writeText(displayPath); setCopyStatus('已复制路径') }
    catch { setCopyStatus('无法复制，可选中路径手动复制') }
  }
  return <ContextPane title="NAS 文件详情" onClose={onClose}>
    {info.isPending ? <LoadingState /> : info.isError ? <ErrorState retry={() => void info.refetch()}>{info.error.message}</ErrorState> : item && <div className="space-y-3">
      <h3 className="break-all font-semibold">{item.name}</h3>
      {!item.is_dir && <Badge>{item.catalog_status === 'unverified' ? '入库身份未核验' : '未纳入资产库'}</Badge>}
      {item.kind === 'image' ? <ImagePreview key={path + item.modified_at} path={path} name={item.name} /> :
        !item.is_dir && <p className="text-sm text-slate-500">此格式可下载后查看</p>}
      <dl className="space-y-1 text-sm">
        <div><dt className="font-medium">类型</dt><dd>{nasType(item)}</dd></div>
        {!item.is_dir && <div><dt className="font-medium">大小</dt><dd>{formatBytes(item.size)}</dd></div>}
        <div><dt className="font-medium">最后修改时间</dt><dd>{nasTime(item.modified_at)}</dd></div>
        <div><dt className="font-medium">路径</dt><dd className="break-all">{displayPath}</dd></div>
      </dl>
      <div className="flex flex-wrap gap-2">
        {item.is_dir ? <Button onClick={() => onOpen(item.path)}>打开目录</Button> :
          <a className="ui-button ui-primary" href={nasDownloadUrl(item.path)}>下载文件</a>}
        <Button primary={false} onClick={() => void copy()}>复制路径</Button>
      </div>
      {copyStatus && <p role="status" className="text-sm text-slate-600">{copyStatus}</p>}
    </div>}
  </ContextPane>
}
