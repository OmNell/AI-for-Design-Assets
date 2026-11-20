import { Button, Panel } from './ui'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getIngest, resumeIngest } from '../api/ingest'

export function IngestPanel({ batchId }: { batchId: number }) {
  const client = useQueryClient()
  const key = ['ingest', batchId]
  const progress = useQuery({
    queryKey: key, queryFn: () => getIngest(batchId),
    refetchInterval: ({ state }) => state.data?.status === '已入库' ? false : 1500,
  })
  const resume = useMutation({
    mutationFn: () => resumeIngest(batchId),
    onSuccess: data => {
      client.setQueryData(key, data)
      void client.invalidateQueries({ queryKey: ['todos'] })
    },
  })
  if (progress.isPending) return <Panel><p role="status">正在读取入库进度…</p></Panel>
  if (progress.isError) return <Panel><p role="alert" className="text-red-800">{progress.error.message}</p></Panel>
  const data = progress.data
  if (!data) return null
  return <Panel>
    <h2 className="text-lg font-semibold">{data.status}</h2>
    <div className="mt-3 grid grid-cols-2 gap-4 text-sm" aria-live="polite">
      <div><p>{data.step === 9 ? data.step_name : `第 ${data.step + 1} 步 · ${data.step_name}`}</p>
        <progress className="mt-2 w-full accent-blue-600" aria-label="入库步骤" max={9} value={data.step} /></div>
      <div><p>{data.products} 个商品 · {data.files} 个文件</p>
        <p className="mt-2 text-slate-600">正式向量 {data.vectors_ready} / {data.products}</p></div>
    </div>
    {data.error && <p className="mt-3 text-sm text-red-800" role="alert">{data.error}</p>}
    {resume.error && <p className="mt-3 text-sm text-red-800" role="alert">{resume.error.message}</p>}
    {data.can_resume && <div className="mt-4"><Button disabled={resume.isPending} onClick={() => resume.mutate()}>继续入库</Button></div>}
  </Panel>
}
