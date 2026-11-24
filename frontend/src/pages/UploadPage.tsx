import { Badge, Button, Dialog, Icon, Panel } from '../components/ui'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { TerminateBatchButton } from '../components/TerminateBatchButton'
import { ThumbnailPreview } from '../components/ThumbnailPreview'
import { checkUpload, createBatch, resumeBatch, getBatch, pauseBatch, type Batch, type Precheck } from '../api/upload'
import { checkSupplement, createSupplement, getOperationOptions } from '../api/operations'
import { canReadPicker, readDrop, readInput, readPicker, type DirectorySelection } from '../lib/dirReader'
import { isIgnored, manifest, preflight, type Issue } from '../lib/preflight'
import { Uploader } from '../lib/uploader'

const formatSize = (value: number) => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB` : `${(value / 1024 ** 2).toFixed(2)} MB`
export function UploadPage() {
  const [params] = useSearchParams()
  const resumeId = Number(params.get('resume')) || null
  const directProject = Number(params.get('project')) || null
  const replaceTarget = Number(params.get('replace')) || null
  const [turnedIntoSupplement, setTurnedIntoSupplement] = useState<number | null>(null)
  const navigate = useNavigate()
  const [won, setWon] = useState<boolean | null>(null)
  const [year, setYear] = useState('')
  const [selection, setSelection] = useState<DirectorySelection | null>(null)
  const [checked, setChecked] = useState<Precheck | null>(null)
  const [issues, setIssues] = useState<Issue[]>([])
  const [acks, setAcks] = useState<string[]>([])
  const [batch, setBatch] = useState<Batch | null>(null)
  const [differences, setDifferences] = useState<Record<string, string[]> | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [directoryStage, setDirectoryStage] = useState<'picking' | 'native' | 'reading' | null>(null)
  const [checkingDirectory, setCheckingDirectory] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [speed, setSpeed] = useState(0)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const cancelPicker = useCallback(() => {
    setDirectoryStage(null)
    setError('浏览器未返回文件夹，可以重新选择或直接拖入。')
  }, [])
  const attachFileInput = useCallback((input: HTMLInputElement | null) => {
    fileInput.current?.removeEventListener('cancel', cancelPicker)
    fileInput.current = input
    input?.addEventListener('cancel', cancelPicker)
  }, [cancelPicker])
  const uploader = useRef<Uploader | null>(null)
  const manualStop = useRef(false)
  const liveBatch = useRef<Batch | null>(null)
  const startTime = useRef(0)
  const initialBytes = useRef(0)
  const projectId = directProject ?? turnedIntoSupplement ?? (batch?.kind === 'supplement' ? batch.project_id : null)
  const replaceQuery = replaceTarget ? `?replace=${replaceTarget}` : ''
  const apply = (value: Batch) => { liveBatch.current = value; setBatch(value) }
  useEffect(() => {
    setSelection(null); setChecked(null); setIssues([]); setAcks([]); setDifferences(null); setBatch(null)
    if (resumeId) getBatch(resumeId).then(value => { apply(value); setWon(value.won) }).catch((reason: Error) => setError(reason.message))
  }, [resumeId])
  useEffect(() => {
    if (!directProject || resumeId) return
    let closed = false
    getOperationOptions(directProject).then(result => { if (!closed) setWon(result.before.project.won) })
      .catch((reason: Error) => { if (!closed) setError(reason.message) })
    return () => { closed = true }
  }, [directProject, resumeId])
  useEffect(() => {
    const leaving = () => {
      if (uploader.current && !uploader.current.stopped && liveBatch.current?.status === '上传中') {
        manualStop.current = true
        uploader.current.stop()
        void pauseBatch(liveBatch.current.id, liveBatch.current.upload_token, true).catch(() => undefined)
      }
    }
    window.addEventListener('pagehide', leaving)
    return () => { leaving(); window.removeEventListener('pagehide', leaving) }
  }, [])
  const supportsFullPicker = canReadPicker()
  const directoryBusy = directoryStage === 'picking' || directoryStage === 'reading'

  const inspect = async (value: DirectorySelection, supplement = projectId) => {
    setError(''); setBusy(true); setCheckingDirectory(true); setSelection(value); setAcks([]); setChecked(null); setDifferences(null); setIssues([])
    try {
      const files = manifest(value.entries)
      const local = preflight({ folder: value.folder_name, entries: files }).filter(issue => !supplement || !['P02', 'P07', 'P08', 'P09'].includes(issue.rule_code))
      setIssues(local)
      const body = { folder_name: value.folder_name, files }
      const server = supplement ? await checkSupplement(supplement, body) : await checkUpload(body, resumeId ?? batch?.id)
      setChecked(server)
      const combined = [...local, ...server.issues]
      setIssues(combined.filter((issue, index) => combined.findIndex(item => item.rule_code === issue.rule_code && item.path === issue.path) === index))
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '预检失败') }
    finally { setBusy(false); setCheckingDirectory(false) }
  }
  const readDirectory = async (
    read: () => DirectorySelection | Promise<DirectorySelection | null>,
    stage: 'picking' | 'reading' = 'reading',
  ) => {
    setError('')
    setDirectoryStage(stage)
    try {
      const value = await read()
      if (value) {
        setDirectoryStage(null)
        await inspect(value)
      }
    } catch (reason: unknown) {
      if (reason instanceof DOMException && ['AbortError', 'NotAllowedError', 'SecurityError', 'NotReadableError', 'NotFoundError'].includes(reason.name)) {
        setError('浏览器未能读取这个文件夹。请确认已允许读取，或使用普通选择、拖入项目文件夹。')
      } else {
        setError(reason instanceof Error && !(reason instanceof DOMException) ? reason.message : '无法读取目录，请重新选择或拖入项目文件夹')
      }
    } finally { setDirectoryStage(null) }
  }
  const pickOrdinary = () => {
    setError('')
    setDirectoryStage('native')
    fileInput.current?.click()
  }
  const pick = () => {
    if (!supportsFullPicker) return pickOrdinary()
    void readDirectory(() => readPicker(() => setDirectoryStage('reading')), 'picking')
  }
  const start = async (value: Batch) => {
    manualStop.current = false
    apply(value); setSelection(null); setConfirm(false); setError(''); setRunning(true)
    startTime.current = performance.now()
    initialBytes.current = value.files.reduce((sum, file) => sum + file.bytes_received, 0)
    if (value.status === '已暂存') { setRunning(false); return }
    const job = new Uploader(value, selection!.entries, update => {
      if (manualStop.current) return
      apply(update)
      const transferred = update.files.reduce((sum, file) => sum + file.bytes_received, 0) - initialBytes.current
      setSpeed(transferred / Math.max((performance.now() - startTime.current) / 1000, 0.1))
    })
    uploader.current = job
    try { const final = await job.run(); if (!manualStop.current) apply(final) }
    catch (reason: unknown) {
      if (!(reason instanceof DOMException && reason.name === 'AbortError')) setError(reason instanceof Error ? reason.message : '上传中断，请继续上传')
      try { if (!manualStop.current) apply(await pauseBatch(value.id, value.upload_token)) } catch { /* 保留已收到的进度；重新登录后继续。 */ }
    }
    finally { setRunning(false) }
  }
  const createOrResume = async (acceptChanges = false) => {
    if (!selection || won === null) return
    setBusy(true); setError('')
    try {
      const body = { folder_name: selection.folder_name, files: manifest(selection.entries), acks }
      const existingId = resumeId ?? (batch && ['上传中', '上传中断'].includes(batch.status) ? batch.id : null)
      if (!existingId && !projectId && checked?.code_status === 'need_h_code' && !validYear) {
        setError('请选择历史项目实际所属年份')
        return
      }
      const value = existingId ? await resumeBatch(existingId, { ...body, accept_changes: acceptChanges }) : projectId ? await createSupplement(projectId, { ...body, won }) : await createBatch({ ...body, won, historical_year: checked?.code_status === 'need_h_code' ? Number(year) : undefined })
      if (value.differences && Object.values(value.differences).some(paths => paths.length) && !acceptChanges) { setDifferences(value.differences); setConfirm(false); return }
      void start(value)
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '无法开始上传') }
    finally { setBusy(false) }
  }
  const pause = async () => {
    manualStop.current = true
    uploader.current?.stop()
    if (liveBatch.current) {
      try { apply(await pauseBatch(liveBatch.current.id, liveBatch.current.upload_token)) }
      catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '暂停失败') }
    }
    setRunning(false)
  }
  const onTerminated = (value: Batch) => {
    manualStop.current = true
    uploader.current?.stop()
    apply(value)
    setRunning(false)
  }
  const total = batch?.files.reduce((sum, file) => sum + file.size, 0) ?? 0
  const received = batch?.files.reduce((sum, file) => sum + file.bytes_received, 0) ?? 0
  const warnings = [...new Set(issues.filter(issue => issue.level === 'warn').map(issue => issue.rule_code))]
  const blocked = issues.some(issue => issue.level === 'block')
  const validYear = /^[0-9]{1,4}$/.test(year) && Number(year) >= 1
  const needsYear = !projectId && checked?.code_status === 'need_h_code' && !batch
  const ready = !busy && checked && !(checked.code_status === 'exists' && !projectId && !resumeId) && !blocked && (!needsYear || validYear) && warnings.every(code => acks.includes(code))
  const choose = !batch || !!selection || batch.status === '上传中断' || batch.status === '上传中' && !running
  const selectionFiles = selection?.entries.filter(e => !e.is_dir && !isIgnored(e)) ?? []
  const step = batch ? batch.status === '已暂存' ? 3 : 2 : selection ? 1 : 0
  return <div className="upload-layout">
    <Panel className="!p-4"><h2 className="font-semibold">入库流程</h2>
      <ol className="mt-4 space-y-3">{['选择项目文件夹', '核对预检结果', '分片上传', '填写主图预描述'].map((title, index) =>
        <li key={title} className={`flex items-start gap-2 text-sm ${step === index ? 'font-semibold text-blue-700' : 'text-slate-600'}`} aria-current={step === index ? 'step' : undefined}>
          <span className={`grid size-5 shrink-0 place-items-center rounded-full text-xs ${step === index ? 'bg-blue-100' : 'bg-slate-100'}`}>{step > index ? <Icon name="check" className="!size-3" /> : index + 1}</span>
          {title}
        </li>)}</ol>
      {batch && <div className="mt-4 space-y-2 border-t border-slate-200 pt-4"><p className="break-all text-xs text-slate-500">{batch.batch_no}</p><Badge tone="info">{batch.queued ? '排队中' : batch.status}</Badge></div>}
    </Panel>
    <div className="min-w-0 space-y-4"><Panel><h1 className="text-2xl font-semibold">{resumeId ? '继续上传' : replaceTarget ? '上传替换主图' : projectId ? '补充已入库项目' : '上传新项目'}</h1>
    <p className="mt-3 text-sm text-slate-500">{projectId ? '请使用目标项目编号命名补充文件夹，只放本次要补充或替换的文件。' : '一次一个项目文件夹；主图优先，资料原样保留。'}</p>
    {error && <p role="alert" className="mt-5 text-red-800">{error}</p>}
    {batch && <p className="mt-4 text-sm text-emerald-800">{batch.batch_no} · {batch.folder_name} · {batch.queued ? '排队中' : batch.status}</p>}
    {!batch && !projectId && <fieldset className="mt-6 flex flex-wrap gap-x-7 gap-y-2"><legend className="mb-3 text-sm">是否中标（必选）</legend>{[true, false].map(value => <label key={String(value)} className="text-sm"><input type="radio" name="won" checked={won === value} onChange={() => setWon(value)} className="mr-2" />{value ? '已中标' : '未中标'}</label>)}</fieldset>}
    {choose && won !== null && <div className="mt-6 rounded-xl border-2 border-dashed border-blue-200 bg-blue-50 px-4 py-9 text-center" onDragOver={event => event.preventDefault()} onDrop={event => {
      event.preventDefault()
      if (!busy && !directoryBusy) void readDirectory(() => readDrop(event.dataTransfer.items))
    }}>
      <p className="mb-4 text-slate-700">{resumeId ? '重新拖入同一项目文件夹，核对续传清单' : '把项目文件夹拖到这里'}</p>
      <div className="flex flex-wrap justify-center gap-2">
        <Button disabled={busy || directoryBusy} onClick={pick}>选择文件夹</Button>
        {supportsFullPicker && <Button primary={false} disabled={busy || directoryBusy} onClick={pickOrdinary}>使用普通选择</Button>}
      </div>
      {(directoryStage || checkingDirectory) && <p role="status" className="mt-3 text-sm text-slate-600">
        {directoryStage === 'picking' || directoryStage === 'native' ? '请在浏览器对话框中确认文件夹并允许读取…' : directoryStage === 'reading' ? '正在读取文件夹…' : '正在预检文件夹…'}
      </p>}
      <input type="file" hidden ref={attachFileInput} {...{ webkitdirectory: '' }}
        onChange={event => {
          const files = Array.from(event.currentTarget.files ?? [])
          event.currentTarget.value = ''
          if (!busy && !directoryBusy) void readDirectory(() => readInput(files))
        }} />
    </div>}
    {selection && !selection.preservesEmptyDirectories && <p role="alert" className="mt-4 text-amber-800">该浏览器的普通文件夹选择无法列出空目录。包含空文件夹时，请改用拖入方式，或使用 Chrome / Edge 目录选择。</p>}
    {selection && needsYear && <label className="mt-5 block text-sm">实际所属年份（必填）<input type="number" min={1} max={9999} required className="field ml-4 max-w-32" value={year} onChange={event => setYear(event.target.value)} /></label>}
  </Panel>
  {selection && <Panel><h2 className="text-xl font-semibold">预检结果</h2><div className="mt-4 grid grid-cols-1 gap-4 2xl:grid-cols-2">
    <div className="max-h-96 overflow-auto rounded-xl bg-slate-50 p-4 font-mono text-xs"><p className={`mb-3 font-semibold ${blocked ? 'text-red-800' : warnings.length ? 'text-amber-700' : 'text-slate-600'}`}>{selection.folder_name}</p>{selection.entries.map(entry => {
      const matching = issues.filter(issue => issue.path === entry.rel_path || entry.rel_path.startsWith(issue.path + '/') || entry.is_dir && issue.path.startsWith(entry.rel_path + '/'))
      const color = matching.some(i => i.level === 'block') ? 'text-red-800' : matching.some(i => i.level === 'warn') ? 'text-amber-700' : matching.some(i => i.level === 'ignore') ? 'text-slate-400' : 'text-slate-600'
      return <p key={entry.rel_path} className={`break-all py-1 ${color}`} style={{ paddingLeft: (entry.rel_path.split('/').length - 1) * 12 }}>{entry.is_dir ? '▸ ' : '· '}{entry.rel_path}</p>
    })}</div>
    <div className="max-h-96 overflow-auto space-y-3">{issues.length ? issues.map((issue, index) => <p key={index} className={`text-sm ${issue.level === 'block' ? 'text-red-800' : issue.level === 'warn' ? 'text-amber-800' : 'text-slate-400'}`}>{issue.level === 'block' ? '拦截' : issue.level === 'warn' ? '提醒' : '忽略'} · {issue.message}<span className="mt-1 block text-xs">{issue.path}</span></p>) : checked && <p className="text-emerald-800">预检通过</p>}</div>
  </div><div className="mt-6 space-y-3">{warnings.map(code => <label key={code} className="block text-sm"><input type="checkbox" checked={acks.includes(code)} onChange={event => setAcks(event.target.checked ? [...acks, code] : acks.filter(v => v !== code))} className="mr-2" />我已知晓：{issues.find(issue => issue.rule_code === code)?.message}</label>)}</div>
    <div className="mt-7">{!projectId && !resumeId && checked?.code_status === 'exists' && checked.project_id && selection ?
      <Button disabled={busy} onClick={() => {
        const existingProject = checked.project_id!
        setTurnedIntoSupplement(existingProject)
        void getOperationOptions(existingProject).then(value => { setWon(value.before.project.won); return inspect(selection, existingProject) })
          .catch((reason: Error) => setError(reason.message))
      }}>补充到这个项目</Button> : blocked ? <p className="text-red-800">有 {issues.filter(i => i.level === 'block').length} 个拦截项，请修改后重新拖入</p> : <Button disabled={!ready} onClick={() => setConfirm(true)}>{resumeId ? '核对清单并继续' : '开始上传'}</Button>}</div>
  </Panel>}
  {differences && <Panel><h2 className="text-xl">续传清单有差异</h2>{Object.entries(differences).map(([key, paths]) => <p key={key} className="mt-3 text-sm">{{ added: '新增', missing: '缺失', changed: '大小或修改时间变化', directories: '目录变化' }[key]}：{paths.join('、') || '无'}</p>)}
    <div className="mt-6"><Button disabled={busy} onClick={() => void createOrResume(true)}>确认差异，变化文件重新上传</Button></div></Panel>}
    </div>
    <div className="min-w-0 space-y-4">{(!batch || selection) && <Panel className="!p-4">
      <div className="flex items-center gap-2"><Icon name="upload" className="text-blue-600" /><h2 className="font-semibold">上传队列</h2></div>
      {selection ? <p className="mt-3 text-slate-600">{selectionFiles.length} 个文件等待{blocked ? '处理预检拦截项' : '确认上传'}</p> :
        <p className="mt-3 text-slate-500">选择项目文件夹后显示文件和进度。</p>}
    </Panel>}
    {batch && !selection && <Panel className="!p-4"><h2 className="font-semibold">上传进度</h2><progress className="mt-6 h-3 w-full accent-blue-600" max={Math.max(total, 1)} value={total ? received : batch.status === '已暂存' ? 1 : 0} />
    <p className="mt-3 text-sm text-slate-600">{formatSize(received)} / {formatSize(total)} · {formatSize(speed)}/秒 · 预计剩余 {speed > 0 ? Math.ceil((total - received) / speed) + ' 秒' : '—'}</p>
    <div className="my-6 max-h-96 overflow-auto text-sm">{batch.files.map(file => <div key={file.id} className="flex flex-wrap items-center justify-between gap-1 border-b border-slate-400/10 py-3"><div className="flex min-w-0 items-center gap-4">
      {!['已撤回', '已终止'].includes(batch.status) && file.upload_status === 'done' && /^(套装|单品)\/[^/]+\.(jpe?g|png)$/i.test(file.rel_path) && <ThumbnailPreview key={`${batch.id}:${file.id}:${file.fingerprint}`} batchId={batch.id} file={file} />}
      <span className="break-all">{file.rel_path}</span></div><span className="shrink-0 text-slate-500">{file.upload_status === 'done' ? '已完成' : file.upload_status === 'partial' ? '上传中' : '等待'} · {formatSize(file.bytes_received)} / {formatSize(file.size)}</span></div>)}</div>
    <div className="flex flex-wrap gap-2">{running && <Button onClick={() => void pause()}>暂停</Button>}{['上传中', '上传中断', '已暂存'].includes(batch.status) && <TerminateBatchButton id={batch.id} disabled={busy} onTerminated={onTerminated} />}
      {batch.status === '上传中断' && <Button onClick={() => { setBatch(null); navigate(`/upload?resume=${batch.id}${replaceTarget ? `&replace=${replaceTarget}` : ''}`) }}>重新拖入继续</Button>}
      {batch.status === '已暂存' && (projectId ? <Link to={`/batches/${batch.id}/operation${replaceQuery}`} className="self-center text-sm text-blue-700 underline">核对补充文件</Link> : <Link to={'/batches/' + batch.id + '/pre-descriptions'} className="ui-button ui-primary">填写主图预描述</Link>)}</div>
    {batch.termination_reason && <p className="mt-5 text-sm text-slate-600">终止原因：{batch.termination_reason}</p>}
    {batch.status === '已暂存' && <p className="mt-5 text-emerald-800">{batch.files.length} 个文件已到达临时区，指纹已计算；空目录已保留。</p>}
    {['AI 处理中', 'AI 处理失败', '待核对'].includes(batch.status) && <p className="mt-5 text-emerald-800"><a href={`/batches/${batch.id}`} className="underline underline-offset-4">查看 AI 进度与结果</a></p>}
  </Panel>}
    </div>
  <Dialog open={confirm} title="确认上传" onClose={() => setConfirm(false)}>
    <div className="space-y-3 text-sm">
      <p>编号：{batch?.project_code ?? (checked?.project_code === 'H' ? `系统将发放 H${year}-四位流水` : checked?.project_code)}</p>
      <p>企业：{checked?.company_name}</p>
      <p>项目：{checked?.project_name}</p>
      <p className="break-all">{won ? '已中标' : '未中标'} · {(won ? checked?.won_path : checked?.lost_path) || '位置待定'}</p>
      <p>{selectionFiles.length} 个文件 · {formatSize(selectionFiles.reduce((sum, file) => sum + (file.file?.size ?? 0), 0))}</p>
      <p>开始传输后将按实际速度估算剩余时间。</p>
      <Button disabled={busy} onClick={() => void createOrResume()}>确认上传</Button>
    </div>
  </Dialog>
  </div>
}
