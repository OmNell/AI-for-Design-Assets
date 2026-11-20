import { useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { confirmBatch, getPreDescriptions, retryAi, savePreDescription,
  type DescriptionState, type PreDescriptionInput, type PreDescriptionProduct } from '../api/upload'
import { retryProductAi } from '../api/review'
import { ImageViewer } from '../components/review/ImageViewer'
import { Thumb } from '../components/review/Thumb'
import { Badge, Button, Panel, Textarea } from '../components/ui'

const labels: Record<DescriptionState, string> = { missing: '待描述', ready: '已填写', reconfirm: '待重新确认' }
const link = 'text-sm text-blue-700 underline underline-offset-4'

export function PreDescriptionPage() {
  const id = Number(useParams().id)
  const [params] = useSearchParams()
  const [current, setCurrent] = useState(Number(params.get('product')) || null)
  const [drafts, setDrafts] = useState<Partial<Record<number, PreDescriptionInput>>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const navigate = useNavigate()
  const cache = useQueryClient()
  const key = ['pre-descriptions', id]
  const valid = Number.isInteger(id) && id > 0
  const query = useQuery({ queryKey: key, queryFn: () => getPreDescriptions(id), enabled: valid, retry: false,
    refetchInterval: result => result.state.data?.products.some(product => product.ai_in_progress) ? 1000 : false })
  if (!valid) return <Panel><p role="alert">批次编号不正确</p></Panel>
  if (query.isPending) return <Panel><p role="status">正在读取主图…</p></Panel>
  if (query.isError) return <Panel><p role="alert">{query.error.message}</p></Panel>

  const { products, status, editable, folder_name: folder, kind } = query.data
  const staged = status === '已暂存'
  const failed = status === 'AI 处理失败'
  const single = status === '待核对' || status === '已驳回'
  const replace = Number(params.get('replace'))
  const suffix = kind === 'supplement' && replace > 0 ? '?replace=' + replace : ''
  const batchPath = '/batches/' + id
  const back = single ? batchPath + '/review' + suffix : batchPath + suffix
  const selected = products.find(product => product.id === current) ?? products.find(product => product.state !== 'ready') ?? products[0]
  const index = products.findIndex(product => product.id === selected?.id)
  const remaining = products.filter(product => product.state !== 'ready')
  const draft = selected ? drafts[selected.id] : undefined
  const text = draft?.description ?? selected?.description ?? ''
  const length = Array.from(text.trim()).length
  const canSave = length >= 2 && length <= 180 && /[\p{L}\p{N}]/u.test(text)
    && (selected?.state !== 'ready' || !!draft)
  const unsaved = Object.keys(drafts).length > 0

  const run = async (action: () => Promise<void>) => {
    if (busy) return
    setBusy(true); setError('')
    try { await action() }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '操作未完成，请重试') }
    finally { setBusy(false) }
  }
  const select = (product: PreDescriptionProduct) => { setCurrent(product.id); setError('') }
  const save = (product: PreDescriptionProduct) => run(async () => {
    const value = await savePreDescription(id, product.id, draft ?? {
      description: text, version: product.version, fingerprint: product.fingerprint,
    })
    cache.setQueryData(key, value)
    setCurrent(product.id)
    setDrafts(previous => { const next = { ...previous }; delete next[product.id]; return next })
    await cache.invalidateQueries({ queryKey: ['review', id] })
  })
  const start = () => run(async () => {
    const result = await (staged ? confirmBatch(id) : retryAi(id))
    cache.setQueryData(['batch', id], result)
    await Promise.all([['batches'], key, ['operation', id]].map(queryKey => cache.invalidateQueries({ queryKey })))
    navigate(result.status === '待核对' ? batchPath + '/review' + suffix : batchPath + suffix)
  })
  const identify = (product: PreDescriptionProduct) => run(async () => {
    await retryProductAi(id, product.id)
    await Promise.all([key, ['batch', id], ['review', id]].map(queryKey => cache.invalidateQueries({ queryKey })))
    navigate(back)
  })

  return <div className="space-y-4">
    <Panel><div className="flex flex-wrap items-start justify-between gap-3">
      <div><Link to={back} className={link}>{single ? '返回人工核对' : '返回批次详情'}</Link>
        <h1 className="mt-2 text-2xl font-semibold">商品主图预描述</h1><p className="mt-1 break-all text-sm text-slate-600">{folder}</p></div>
      <div className="text-right"><p role="status" className="mb-2 text-sm font-medium text-slate-700">已完成 {products.length - remaining.length} / {products.length} 张主图</p>
        {(staged || failed) && editable && <Button disabled={busy || remaining.length > 0 || unsaved}
          onClick={() => void start()}>{staged ? products.length ? '确认全部描述并开始 AI' : '确认资料并继续核对' : '重试整批 AI'}</Button>}</div>
    </div>
    {remaining.length > 0 && <p className="mt-3 break-words text-sm text-amber-800">还需填写或确认：{remaining.map(product => product.display_name).join('、')}</p>}
    {unsaved && <p className="mt-2 text-sm text-amber-800">有未保存的描述。</p>}
    {error && <p role="alert" className="mt-2 text-sm text-red-800">{error}</p>}
    </Panel>
    {selected ? <div className="grid min-w-0 grid-cols-[minmax(190px,1fr)_minmax(300px,2fr)_minmax(260px,1.3fr)] gap-4">
      <Panel><div className="max-h-[65vh] space-y-4 overflow-y-auto">
        {(['item', 'set'] as const).map(type => {
          const group = products.filter(product => product.kind === type)
          return group.length > 0 && <section key={type}><h2 className="mb-2 text-sm font-semibold">{type === 'item' ? '单品主图' : '套装主图'}</h2>
            <ul className="space-y-1">{group.map(product => <li key={product.id}><button type="button"
              className={'flex w-full items-center gap-2 rounded-lg px-1 py-2 text-left ' + (selected.id === product.id ? 'bg-blue-50 text-blue-800' : 'hover:bg-slate-50')}
              aria-pressed={selected.id === product.id} disabled={busy} onClick={() => select(product)}>
              <Thumb batchId={id} fileId={product.batch_file_id} version={product.fingerprint ?? ''} alt="" />
              <span className="min-w-0"><span className="block truncate text-sm" title={product.display_name}>{product.display_name}</span>
                <span className="text-xs text-slate-600">{drafts[product.id] ? '未保存' : labels[product.state]}</span></span></button></li>)}</ul></section>
        })}
      </div></Panel>
      <Panel><div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="truncate text-sm font-semibold" title={selected.display_name}>{selected.display_name}</h2>
        <div className="flex shrink-0 gap-2">
          <Button primary={false} disabled={busy || index <= 0} onClick={() => select(products[index - 1])}>上一张</Button>
          <Button primary={false} disabled={busy || index >= products.length - 1} onClick={() => select(products[index + 1])}>下一张</Button>
        </div>
      </div><div className="h-[55vh] min-h-80"><ImageViewer batchId={id} fileId={selected.batch_file_id}
        version={selected.fingerprint ?? ''} name={selected.display_name} /></div></Panel>
      <Panel><div className="space-y-3">
        <Badge tone={selected.state === 'ready' ? 'success' : 'warning'}>{labels[selected.state]}</Badge>
        <label htmlFor="product-description" className="block text-lg font-semibold">这张图是什么产品？</label>
        <p id="description-help" className="text-sm text-slate-600">请用一句话描述产品的真实品类、用途或组成。即使产品采用特殊造型，也请说明它实际上是什么。</p>
        <Textarea id="product-description" aria-describedby="description-help" rows={5} value={text}
          disabled={!editable || selected.ai_in_progress || busy}
          placeholder={selected.kind === 'set' ? '这是一套由茶壶、茶杯和茶盘组成的茶具礼盒。' : '这是一款磁带造型的蓝牙音箱。'}
          onChange={event => {
            const description = event.target.value
            if (Array.from(description).length > 180) return
            setDrafts(previous => {
              const next = { ...previous }
              if (description === (selected.description ?? '')) delete next[selected.id]
              else next[selected.id] = { ...(previous[selected.id] ?? {
                version: selected.version, fingerprint: selected.fingerprint,
              }), description }
              return next
            })
          }} />
        <p className="text-xs text-slate-500">{length} / 180 字。请只描述产品，避免填写账号、企业名、编号和文件路径。</p>
        {selected.state === 'reconfirm' && <p className="text-sm text-amber-800">主图已变更，请核对现有文字后重新保存。</p>}
        {selected.ai_in_progress && <p role="status" className="text-sm text-slate-600">识别完成后可以修改描述。</p>}
        {selected.visual_ai_succeeded && editable && <p className="text-sm text-slate-600">改写并保存后，需要重新识别并逐项确认标签。</p>}
        {selected.ai_error && <p className="text-sm text-red-800">{selected.ai_error}</p>}
        {editable && <div className="flex flex-wrap gap-2">
          <Button disabled={busy || selected.ai_in_progress || !canSave} onClick={() => void save(selected)}>
            {selected.state === 'reconfirm' ? '确认此图的描述' : '保存描述'}</Button>
          {single && selected.state === 'ready' && <Button primary={false}
            disabled={busy || !!draft || selected.ai_in_progress} onClick={() => void identify(selected)}>
            {selected.ai_status === 'failed' || selected.visual_ai_succeeded ? '重新识别这张图' : '识别这张图'}</Button>}
        </div>}
      </div></Panel>
    </div> : <Panel><p className="text-sm text-slate-600">本批次没有需要描述的新主图。</p></Panel>}
  </div>
}
