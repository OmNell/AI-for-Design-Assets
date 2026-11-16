import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { GlassModal } from '../components/glass/GlassModal'
import { checkUpload, createBatch, resumeBatch, getBatch, pauseBatch, cancelBatch, confirmBatch, type Batch, type Precheck } from '../api/upload'
import { readDrop, readInput, readPicker, type DirectorySelection } from '../lib/dirReader'
import { isIgnored, manifest, preflight, type Issue } from '../lib/preflight'
import { Uploader } from '../lib/uploader'

const formatSize = (value: number) => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GB` : `${(value / 1024 ** 2).toFixed(2)} MB`
export function UploadPage() {
  const [params] = useSearchParams()
  const resumeId = Number(params.get('resume')) || null
  const navigate = useNavigate()
  const [won, setWon] = useState<boolean | null>(null)
  const [year, setYear] = useState(new Date().getFullYear())
  const [selection, setSelection] = useState<DirectorySelection | null>(null)
  const [checked, setChecked] = useState<Precheck | null>(null)
  const [issues, setIssues] = useState<Issue[]>([])
  const [acks, setAcks] = useState<string[]>([])
  const [batch, setBatch] = useState<Batch | null>(null)
  const [differences, setDifferences] = useState<Record<string, string[]> | null>(null)
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState('')
  const [speed, setSpeed] = useState(0)
  const fileInput = useRef<HTMLInputElement>(null)
  const uploader = useRef<Uploader | null>(null)
  const manualStop = useRef(false)
  const liveBatch = useRef<Batch | null>(null)
  const startTime = useRef(0)
  const initialBytes = useRef(0)
  const apply = (value: Batch) => { liveBatch.current = value; setBatch(value) }
  useEffect(() => {
    setSelection(null); setChecked(null); setIssues([]); setAcks([]); setDifferences(null); setBatch(null)
    if (resumeId) getBatch(resumeId).then(value => { apply(value); setWon(value.won) }).catch((reason: Error) => setError(reason.message))
  }, [resumeId])
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

  const inspect = async (value: DirectorySelection) => {
    setError(''); setBusy(true); setSelection(value); setAcks([]); setChecked(null); setDifferences(null)
    const files = manifest(value.entries)
    const local = preflight({ folder: value.folder_name, entries: files })
    setIssues(local)
    try {
      const server = await checkUpload({ folder_name: value.folder_name, files })
      setChecked(server)
      const combined = [...local, ...server.issues]
      setIssues(combined.filter((issue, index) => combined.findIndex(item => item.rule_code === issue.rule_code && item.path === issue.path) === index))
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '预检失败') }
    finally { setBusy(false) }
  }
  const pick = async () => {
    try { const value = await readPicker(); if (value) await inspect(value); else fileInput.current?.click() }
    catch (reason: unknown) { if (!(reason instanceof DOMException && reason.name === 'AbortError')) setError(reason instanceof Error ? reason.message : '无法读取目录') }
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
      const value = existingId ? await resumeBatch(existingId, { ...body, accept_changes: acceptChanges }) : await createBatch({ ...body, won, historical_year: year })
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
  const cancel = async () => {
    manualStop.current = true
    uploader.current?.stop(); setBusy(true)
    try { if (liveBatch.current) apply(await cancelBatch(liveBatch.current.id, liveBatch.current.upload_token)); setRunning(false) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '取消失败，请重试') }
    finally { setBusy(false) }
  }
  const total = batch?.files.reduce((sum, file) => sum + file.size, 0) ?? 0
  const received = batch?.files.reduce((sum, file) => sum + file.bytes_received, 0) ?? 0
  const warnings = [...new Set(issues.filter(issue => issue.level === 'warn').map(issue => issue.rule_code))]
  const blocked = issues.some(issue => issue.level === 'block')
  const ready = !busy && checked && !blocked && warnings.every(code => acks.includes(code))
  const choose = !batch || !!selection || batch.status === '上传中断' || batch.status === '上传中' && !running
  const selectionFiles = selection?.entries.filter(e => !e.is_dir && !isIgnored(e)) ?? []
  return <div className="space-y-6"><GlassPanel><h1 className="text-2xl font-semibold">{resumeId ? '继续上传' : '上传新项目'}</h1>
    <p className="mt-3 text-sm text-slate-500">一次一个项目文件夹；主图优先，资料原样保留。</p>
    {error && <p role="alert" className="mt-5 text-red-800">{error}</p>}
    {batch && <p className="mt-4 text-sm text-emerald-800">{batch.batch_no} · {batch.folder_name} · {batch.queued ? '排队中' : batch.status}</p>}
    {!batch && <fieldset className="mt-6 flex gap-7"><legend className="mb-3 text-sm">是否中标（必选）</legend>{[true, false].map(value => <label key={String(value)} className="text-sm"><input type="radio" name="won" checked={won === value} onChange={() => setWon(value)} className="mr-2" />{value ? '已中标' : '未中标'}</label>)}</fieldset>}
    {choose && won !== null && <div className="mt-7 rounded-2xl border-2 border-dashed border-emerald-800/25 bg-white/15 p-10 text-center" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); void readDrop(event.dataTransfer.items).then(inspect).catch((reason: Error) => setError(reason.message)) }}>
      <p className="mb-5 text-slate-600">{resumeId ? '重新拖入同一项目文件夹，核对续传清单' : '把项目文件夹拖到这里'}</p><GlassButton disabled={busy} onClick={pick}>选择文件夹</GlassButton>
      <input type="file" hidden ref={fileInput} {...{ webkitdirectory: '' }} multiple onChange={event => { if (event.target.files?.length) { try { void inspect(readInput(event.target.files)) } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '目录读取失败') } } event.target.value = '' }} />
    </div>}
    {selection && !selection.preservesEmptyDirectories && <p role="alert" className="mt-4 text-amber-800">该浏览器的普通文件夹选择无法列出空目录。包含空文件夹时，请改用拖入方式，或使用 Chrome / Edge 目录选择。</p>}
    {selection && checked?.code_status === 'need_h_code' && !batch && <label className="mt-5 block text-sm">历史项目年份<input type="number" min={1} max={9999} className="field ml-4 max-w-32" value={year} onChange={event => setYear(Number(event.target.value))} /></label>}
  </GlassPanel>
  {selection && <GlassPanel><h2 className="text-xl font-semibold">预检结果</h2><div className="mt-6 grid grid-cols-2 gap-8">
    <div className="max-h-96 overflow-auto rounded-xl bg-white/20 p-4 font-mono text-xs"><p className={`mb-3 font-semibold ${blocked ? 'text-red-800' : warnings.length ? 'text-amber-700' : 'text-slate-600'}`}>{selection.folder_name}</p>{selection.entries.map(entry => {
      const matching = issues.filter(issue => issue.path === entry.rel_path || entry.rel_path.startsWith(issue.path + '/') || entry.is_dir && issue.path.startsWith(entry.rel_path + '/'))
      const color = matching.some(i => i.level === 'block') ? 'text-red-800' : matching.some(i => i.level === 'warn') ? 'text-amber-700' : matching.some(i => i.level === 'ignore') ? 'text-slate-400' : 'text-slate-600'
      return <p key={entry.rel_path} className={`py-1 ${color}`} style={{ paddingLeft: (entry.rel_path.split('/').length - 1) * 12 }}>{entry.is_dir ? '▸ ' : '· '}{entry.rel_path}</p>
    })}</div>
    <div className="max-h-96 overflow-auto space-y-3">{issues.length ? issues.map((issue, index) => <p key={index} className={`text-sm ${issue.level === 'block' ? 'text-red-800' : issue.level === 'warn' ? 'text-amber-800' : 'text-slate-400'}`}>{issue.level === 'block' ? '拦截' : issue.level === 'warn' ? '提醒' : '忽略'} · {issue.message}<span className="mt-1 block text-xs">{issue.path}</span></p>) : <p className="text-emerald-800">预检通过</p>}</div>
  </div><div className="mt-6 space-y-3">{warnings.map(code => <label key={code} className="block text-sm"><input type="checkbox" checked={acks.includes(code)} onChange={event => setAcks(event.target.checked ? [...acks, code] : acks.filter(v => v !== code))} className="mr-2" />我已知晓：{issues.find(issue => issue.rule_code === code)?.message}</label>)}</div>
    <div className="mt-7">{blocked ? <p className="text-red-800">有 {issues.filter(i => i.level === 'block').length} 个拦截项，请修改后重新拖入</p> : <GlassButton disabled={!ready} onClick={() => setConfirm(true)}>{resumeId ? '核对清单并继续' : '开始上传'}</GlassButton>}</div>
  </GlassPanel>}
  {differences && <GlassPanel><h2 className="text-xl">续传清单有差异</h2>{Object.entries(differences).map(([key, paths]) => <p key={key} className="mt-3 text-sm">{{ added: '新增', missing: '缺失', changed: '大小或修改时间变化', directories: '目录变化' }[key]}：{paths.join('、') || '无'}</p>)}
    <div className="mt-6"><GlassButton disabled={busy} onClick={() => void createOrResume(true)}>确认差异，变化文件重新上传</GlassButton></div></GlassPanel>}
  {batch && !selection && <GlassPanel><h2 className="text-xl font-semibold">上传进度</h2><progress className="mt-6 h-3 w-full accent-emerald-700" max={Math.max(total, 1)} value={total ? received : batch.status === '已暂存' ? 1 : 0} />
    <p className="mt-3 text-sm text-slate-600">{formatSize(received)} / {formatSize(total)} · {formatSize(speed)}/秒 · 预计剩余 {speed > 0 ? Math.ceil((total - received) / speed) + ' 秒' : '—'}</p>
    <div className="my-6 max-h-96 overflow-auto text-sm">{batch.files.map(file => <div key={file.id} className="flex justify-between gap-5 border-b border-slate-400/10 py-3"><span className="break-all">{file.rel_path}</span><span className="shrink-0 text-slate-500">{file.upload_status === 'done' ? '已完成' : file.upload_status === 'partial' ? '上传中' : '等待'} · {formatSize(file.bytes_received)} / {formatSize(file.size)}</span></div>)}</div>
    <div className="flex gap-4">{running && <GlassButton onClick={() => void pause()}>暂停</GlassButton>}{['上传中', '上传中断', '已暂存'].includes(batch.status) && <GlassButton disabled={busy} onClick={() => void cancel()}>取消上传</GlassButton>}
      {batch.status === '上传中断' && <GlassButton onClick={() => { setBatch(null); navigate(`/upload?resume=${batch.id}`) }}>重新拖入继续</GlassButton>}
      {batch.status === '已暂存' && <GlassButton onClick={() => { void confirmBatch(batch.id).then(apply).catch((reason: Error) => setError(reason.message)) }}>确认，开始 AI 处理</GlassButton>}</div>
    {batch.status === '已暂存' && <p className="mt-5 text-emerald-800">{batch.files.length} 个文件已到达临时区，指纹已计算；空目录已保留。</p>}
    {batch.status === 'AI 处理中' && <p role="status" className="mt-5 text-emerald-800">AI 处理功能尚未开放</p>}
  </GlassPanel>}
  <GlassModal open={confirm} title="确认上传" onClose={() => setConfirm(false)}><div className="space-y-3 text-sm"><p>编号：{batch?.project_code ?? (checked?.project_code === 'H' ? `系统将发放 H${year}-四位流水` : checked?.project_code)}</p><p>企业：{checked?.company_name}</p><p>项目：{checked?.project_name}</p><p>{won ? '已中标' : '未中标'} · 位置待定</p><p>{selectionFiles.length} 个文件 · {formatSize(selectionFiles.reduce((sum, file) => sum + (file.file?.size ?? 0), 0))}</p><GlassButton disabled={busy} onClick={() => void createOrResume()}>确认上传</GlassButton></div></GlassModal>
  </div>
}
