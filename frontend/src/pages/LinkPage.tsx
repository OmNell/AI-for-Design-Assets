import { useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router'
import { useQuery } from '@tanstack/react-query'
import { checkFile, getLink } from '../api/catalog'
import { downloadMessage, triggerDownload } from '../lib/download'
import { GlassPanel } from '../components/glass/GlassPanel'

/** 永久链接落地页：商品和项目直接跳转；文件触发下载；已作废或已删除的目标显示提示。 */
export function LinkPage() {
  const linkId = useParams().linkId ?? ''
  const link = useQuery({ queryKey: ['link', linkId], queryFn: () => getLink(linkId), retry: false })
  const [state, setState] = useState<{ name?: string; error?: string }>({})
  const target = link.data
  useEffect(() => {
    if (target?.kind !== 'file' || !target.url) return
    let cancelled = false
    const url = target.url
    checkFile(target.target_id).then(file => { if (!cancelled) { setState({ name: file.name }); triggerDownload(url) } })
      .catch((reason: unknown) => { if (!cancelled) setState({ error: downloadMessage(reason) }) })
    return () => { cancelled = true }
  }, [target?.kind, target?.url, target?.target_id])
  if (link.isPending) return <GlassPanel><p>正在打开链接…</p></GlassPanel>
  if (link.isError) return <GlassPanel><p role="alert" className="text-red-800">{link.error.message}</p><Link to="/" className="mt-4 inline-block text-sm text-emerald-800 underline underline-offset-4">返回首页</Link></GlassPanel>
  if (!target!.url) return <GlassPanel><h1 className="text-xl font-semibold">{target!.message}</h1><p className="mt-3 text-sm text-slate-500">这个永久链接指向的内容已不再有效。</p><Link to="/projects" className="mt-4 inline-block text-sm text-emerald-800 underline underline-offset-4">去项目列表</Link></GlassPanel>
  if (target!.kind !== 'file') return <Navigate to={target!.url!} replace />
  return <GlassPanel>{state.error ? <p role="alert" className="text-red-800">{state.error}</p> : <p role="status">{state.name ? `已开始下载：${state.name}` : '正在检查文件…'}</p>}
    <Link to="/projects" className="mt-4 inline-block text-sm text-emerald-800 underline underline-offset-4">去项目列表</Link></GlassPanel>
}
