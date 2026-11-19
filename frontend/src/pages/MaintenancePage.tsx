import { useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import {
  ackFinding, backupDownload, createRepairs, executeCleanup, exportLogs, getBackups, getCleanup, getErrors,
  getFindings, getOpsLogs, getRepairs, getRuns, getSearchLogs, getSystem, previewCleanup, rebuildThumbs,
  retrySystem, runMaintenance, saveSystem, type CleanupPreview, type SystemJob, type SystemParameters,
} from '../api/maintenance'
import { AdminLinks } from '../components/AdminLinks'
import { GlassButton } from '../components/glass/GlassButton'
import { GlassPanel } from '../components/glass/GlassPanel'
import { formatBytes } from '../lib/terms'

const linkClass = 'text-sm text-emerald-800 underline underline-offset-4'
const tabs = { inspection: '巡检报告', backup: '备份', cleanup: '临时区清理', errors: '异常任务', logs: '操作日志', search: '搜索日志', settings: '系统参数', tools: '维护工具' }
const kindNames: Record<string, string> = { moved: '违规移动', modified: '内容变化', unregistered: '未登记', missing: '文件丢失',
  archive_broken: '档案异常', inspection: '巡检', backup: '备份', catalog_thumb: '缩略图重建',
  embed_index: '向量候选索引', embedding: '商品向量', ai_stalled: 'AI 处理停滞', ai_tag_product: 'AI 识图' }
const time = (value: string | null) => value ? new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z').toLocaleString('zh-CN') : '—'
const message = (reason: unknown) => reason instanceof Error ? reason.message : '操作失败，请稍后重试'

function Load({ loading, error, children }: { loading: boolean; error: unknown; children: ReactNode }) {
  if (loading) return <p role="status" className="text-sm">正在读取…</p>
  if (error) return <p role="alert" className="text-red-800">{message(error)}</p>
  return <>{children}</>
}
function Table({ columns, children }: { columns: string[]; children: ReactNode }) {
  return <div className="overflow-auto"><table className="w-full border-collapse text-left text-sm">
    <thead className="border-b border-slate-300"><tr>{columns.map(label => <th className="px-3 py-2" key={label}>{label}</th>)}</tr></thead>
    <tbody className="[&_td]:p-3 [&_tr]:border-b [&_tr]:border-slate-300/50">{children}</tbody>
  </table></div>
}
function Quiet({ children }: { children: ReactNode }) { return <p className="text-sm text-slate-600">{children}</p> }

function Trigger({ kind }: { kind: SystemJob }) {
  const cache = useQueryClient()
  const job = useMutation({ mutationFn: () => runMaintenance(kind),
    onSuccess: () => { void cache.invalidateQueries({ queryKey: ['maintenance'] }) } })
  return <div className="flex items-center gap-3">
    <GlassButton disabled={job.isPending} onClick={() => job.mutate()}>立即{kind === 'backup' ? '备份' : '巡检'}</GlassButton>
    {job.isSuccess && <span role="status" className="text-sm">任务已进入队列</span>}
    {job.error && <span role="alert" className="text-sm text-red-800">{message(job.error)}</span>}
  </div>
}

function Inspection() {
  const [run, setRun] = useState('')
  const [kind, setKind] = useState('')
  const [project, setProject] = useState('')
  const [state, setState] = useState('all')
  const [page, setPage] = useState(1)
  const cache = useQueryClient()
  const runs = useQuery({ queryKey: ['maintenance', 'runs'], queryFn: getRuns, refetchInterval: 4000 })
  const params = new URLSearchParams({ status: state, page: String(page) })
  if (run) params.set('run_id', run)
  if (kind) params.set('kind', kind)
  if (project) params.set('project', project)
  const findings = useQuery({ queryKey: ['maintenance', 'findings', params.toString()], queryFn: () => getFindings(params.toString()),
    refetchInterval: runs.data?.some(row => ['running', 'queued'].includes(row.status)) ? 4000 : false })
  const ack = useMutation({ mutationFn: ackFinding, onSuccess: () => { void cache.invalidateQueries({ queryKey: ['maintenance', 'findings'] }); void cache.invalidateQueries({ queryKey: ['todos'] }) } })
  return <div className="space-y-4">
    <Trigger kind="inspection" />
    <Load loading={runs.isPending} error={runs.error}>
      <Table columns={['运行', '状态', '扫描项目／文件', '异常／自动纠正', '耗时', '失败原因']}>
        {runs.data?.map(row => <tr key={row.id}><td><button className={linkClass} onClick={() => { setRun(String(row.id)); setPage(1) }}>#{row.id} {time(row.started_at)}</button></td>
          <td>{row.status}</td><td>{row.summary.projects ?? '—'}／{row.summary.files ?? '—'}</td>
          <td>{row.summary.count ?? '—'}／{row.summary.auto_fixed ?? '—'}
            {!!row.summary.kinds && <small className="block">{Object.entries(row.summary.kinds).map(([key, count]) => `${kindNames[key] ?? key} ${count}`).join(' · ')}</small>}</td>
          <td>{row.summary.elapsed_ms ? `${row.summary.elapsed_ms} ms` : '—'}</td><td>{row.error ?? '—'}</td></tr>)}
      </Table>{runs.data?.length === 0 && <Quiet>尚无巡检报告。</Quiet>}
    </Load>
    <div className="grid grid-cols-4 gap-2">
      <label className="text-sm">巡检批次<select className="field" value={run} onChange={event => { setRun(event.target.value); setPage(1) }}>
        <option value="">全部批次</option>{runs.data?.map(row => <option key={row.id} value={row.id}>#{row.id}</option>)}</select></label>
      <label className="text-sm">异常类型<select className="field" value={kind} onChange={event => { setKind(event.target.value); setPage(1) }}>
        <option value="">全部类型</option>{['moved', 'modified', 'unregistered', 'missing', 'archive_broken'].map(value => <option key={value} value={value}>{kindNames[value]}</option>)}</select></label>
      <label className="text-sm">项目 ID<input className="field" type="number" min={1} value={project} onChange={event => { setProject(event.target.value); setPage(1) }} /></label>
      <label className="text-sm">处理状态<select className="field" value={state} onChange={event => { setState(event.target.value); setPage(1) }}>
        <option value="all">全部</option><option value="open">待知悉</option><option value="ack">已知悉</option><option value="repaired">已修复</option></select></label>
    </div>
    <Load loading={findings.isPending} error={findings.error}>
      <Table columns={['时间与类型', '实际路径', '详情与处理记录', '操作']}>{findings.data?.map(item => <tr key={item.id}>
        <td>{time(item.created_at)}<br />{kindNames[item.kind] ?? item.kind}</td><td className="break-all">{item.path}</td>
        <td><details><summary className="cursor-pointer">{item.auto_fixed ? '已纠正实际路径' : item.resolved_at ? '已修复' : item.acknowledged_at ? '已知悉' : '查看详情'}</summary>
          <p className="break-all">{item.detail}</p>{item.acknowledged_at && <p>账号 #{item.acknowledged_by} · {time(item.acknowledged_at)}</p>}</details></td>
        <td>{item.project_id && <Link className={linkClass} to={`/projects/${item.project_id}`}>项目</Link>}
          {!item.acknowledged_at && !item.resolved_at && <GlassButton primary={false} disabled={ack.isPending} onClick={() => ack.mutate(item.id)}>标记已知悉</GlassButton>}</td>
      </tr>)}</Table>
      {findings.data?.length === 0 && <Quiet>没有符合筛选条件的发现。</Quiet>}
      <Pager page={page} setPage={setPage} more={findings.data?.length === 100} />
    </Load>
    {ack.error && <p role="alert" className="text-red-800">{message(ack.error)}</p>}
  </div>
}

function Backups() {
  const backups = useQuery({ queryKey: ['maintenance', 'backup'], queryFn: getBackups, refetchInterval: 5000 })
  return <div className="space-y-4"><Trigger kind="backup" />
    <Quiet>归档包含用于恢复的密钥。仅授权管理员可下载，请存放于受限位置。</Quiet>
    <Load loading={backups.isPending} error={backups.error}>
      <Table columns={['备份包', '大小', '状态', '验证时间', '下载']}>{backups.data?.map(row => <tr key={row.id}>
        <td>{row.name}</td><td>{row.size === null ? '—' : formatBytes(row.size)}</td><td>{row.error ?? row.status}</td><td>{time(row.verified_at)}</td>
        <td>{row.verified_at && <a className={linkClass} href={backupDownload(row.id)}>下载已验证备份</a>}</td></tr>)}</Table>
      {backups.data?.length === 0 && <Quiet>尚无备份记录。</Quiet>}
    </Load></div>
}

function Cleanup() {
  const cache = useQueryClient()
  const [code, setCode] = useState('')
  const [plan, setPlan] = useState<CleanupPreview | null>(null)
  const targets = useQuery({ queryKey: ['maintenance', 'cleanup'], queryFn: getCleanup })
  const preview = useMutation({ mutationFn: previewCleanup, onSuccess: value => { setCode(''); setPlan(value) } })
  const execute = useMutation({ mutationFn: () => executeCleanup(plan!.id, code), onSuccess: () => {
    setPlan(null); setCode(''); void cache.invalidateQueries({ queryKey: ['maintenance', 'cleanup'] })
  } })
  return <div className="space-y-4">
    <Quiet>只清理终态批次或已核实的孤儿文件，按当前管理员的 NAS 权限执行。</Quiet>
    <Load loading={targets.isPending} error={targets.error}><Table columns={['批次／文件', '项目及责任人', '状态', '占用', '最后活动', '操作']}>
      {targets.data?.map(row => <tr key={row.key}><td>{row.label}<br /><span className="text-xs text-slate-600">{row.kind}</span></td>
        <td>{row.project_code ?? '—'} · {row.owner ?? '—'}</td><td>{row.status}</td><td>{row.files} 个 · {formatBytes(row.size)}</td><td>{time(row.updated_at)}</td>
        <td><GlassButton primary={false} disabled={!row.eligible || preview.isPending || execute.isPending} onClick={() => { execute.reset(); preview.mutate(row.key) }}>核对清单</GlassButton></td>
      </tr>)}</Table>
      {targets.data?.length === 0 && <Quiet>临时区没有可显示的文件。</Quiet>}
    </Load>
    {plan && <section className="space-y-3 rounded-xl border border-red-200 bg-red-50 p-4">
      <h2 className="font-semibold">清理清单 · {plan.entries.length} 个文件 · {formatBytes(plan.entries.reduce((sum, row) => sum + row.size, 0))}</h2>
      <ul className="max-h-56 overflow-auto break-all text-sm">{plan.entries.map(row => <li key={row.path}>{row.path} · {formatBytes(row.size)}</li>)}</ul>
      <label className="block text-sm">输入 <strong>{plan.target}</strong> 二次确认<input className="field mt-2" value={code} onChange={event => setCode(event.target.value)} /></label>
      <GlassButton disabled={code !== plan.target || execute.isPending} onClick={() => execute.mutate()}>确认清理</GlassButton>
      <button className={linkClass + ' ml-3'} onClick={() => setPlan(null)}>取消</button>
    </section>}
    {execute.isSuccess && <p role="status" className="text-sm">确认的临时文件已清理。</p>}
    {(preview.error || execute.error) && <p role="alert" className="text-red-800">{message(preview.error ?? execute.error)}</p>}
  </div>
}

function Pager({ page, setPage, more }: { page: number; setPage: (page: number) => void; more?: boolean }) {
  return <div className="mt-3 flex items-center gap-3 text-sm">
    <GlassButton primary={false} disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</GlassButton>
    <span>第 {page} 页</span><GlassButton primary={false} disabled={!more} onClick={() => setPage(page + 1)}>下一页</GlassButton>
  </div>
}

function Logs({ search }: { search: boolean }) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [page, setPage] = useState(1)
  const parameters = new URLSearchParams({ ...filters, page: String(page) })
  const ops = useQuery({ queryKey: ['maintenance', 'oplog', parameters.toString()], queryFn: () => getOpsLogs(parameters.toString()), enabled: !search })
  const searches = useQuery({ queryKey: ['maintenance', 'search-logs', parameters.toString()], queryFn: () => getSearchLogs(parameters.toString()), enabled: search })
  const state = search ? searches : ops
  const fields: Array<[string, string, string]> = [['since', '开始时间', 'datetime-local'], ['until', '结束时间', 'datetime-local'], ['user_id', '操作人 ID', 'number']]
  if (!search) fields.push(['action', '动作', 'text'], ['object_type', '对象类型', 'text'], ['object_id', '对象 ID', 'text'], ['result', '结果', 'text'])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    setFilters(Object.fromEntries(Object.entries(values).filter(([, value]) => value).map(([key, value]) =>
      [key, ['since', 'until'].includes(key) ? new Date(value).toISOString() : value])))
    setPage(1)
  }
  return <div className="space-y-4">
    <form onSubmit={submit} className="grid grid-cols-4 items-end gap-3">{fields.map(([key, label, type]) =>
      <label className="text-sm" key={key}>{label}<input className="field mt-1" type={type} value={values[key] ?? ''} onChange={event => setValues({ ...values, [key]: event.target.value })} /></label>)}
      {search && <label className="text-sm"><input type="checkbox" checked={values.unclicked === 'true'} onChange={event => setValues({ ...values, unclicked: event.target.checked ? 'true' : '' })} /> 只看无点击记录</label>}
      <div className="flex items-center gap-3"><GlassButton type="submit">查询</GlassButton><a className={linkClass} href={exportLogs(search, parameters.toString())}>{search ? '导出无点击查询' : '导出 CSV'}</a></div>
    </form>
    {search && <Quiet>无点击记录可用于整理检索题目，单凭这一项无法判断检索质量。</Quiet>}
    <Load loading={state.isPending} error={state.error}>
      {search ? <Table columns={['查询时间', '账号', '查询', '结果／点击', '耗时', '降级提示']}>{searches.data?.items.map(row => <tr key={row.id}>
        <td>{time(row.ts)}</td><td>{row.username ?? row.user_id}</td><td>{row.query}</td><td>{row.result_count}／{row.clicks}</td>
        <td>{row.duration_ms} ms</td><td>{row.warnings.join('、') || '—'}</td></tr>)}</Table>
        : <Table columns={['操作时间', '账号／角色', '动作', '对象', '结果／来源', '详情']}>{ops.data?.items.map(row => <tr key={row.id}>
          <td>{time(row.ts)}</td><td>{row.username ?? row.user_id ?? '系统'}／{row.role}</td><td>{row.action}</td>
          <td>{row.object_type} #{row.object_id ?? '—'}</td><td>{row.result}／{row.client_host}</td>
          <td><details><summary>查看</summary><pre className="max-w-sm whitespace-pre-wrap break-all text-xs">{JSON.stringify({ 变更前: row.before, 变更后: row.after }, null, 2)}</pre></details></td></tr>)}</Table>}
      <p className="mt-2 text-sm">共 {state.data?.total ?? 0} 条</p>
      <Pager page={page} setPage={setPage} more={page * 50 < (state.data?.total ?? 0)} />
    </Load>
  </div>
}

function Errors() {
  const cache = useQueryClient()
  const errors = useQuery({ queryKey: ['maintenance', 'errors'], queryFn: getErrors })
  const retry = useMutation({ mutationFn: retrySystem, onSuccess: () => { void cache.invalidateQueries({ queryKey: ['maintenance'] }) } })
  return <Load loading={errors.isPending} error={errors.error}>
    <Table columns={['异常', '原因', '相关对象', '处理入口']}>{errors.data?.map((row, index) => <tr key={`${row.kind}-${row.object_id}-${index}`}>
      <td>{kindNames[row.kind] ?? row.kind}</td><td className="break-all">{row.reason}</td>
      <td>{row.batch_id ? <Link className={linkClass} to={`/batches/${row.batch_id}`}>批次 #{row.batch_id}</Link> : `#${row.object_id}`}</td>
      <td>{row.retry === 'system' && row.job_id && <GlassButton primary={false} disabled={retry.isPending} onClick={() => retry.mutate(row.job_id!)}>重试后台任务</GlassButton>}
        {row.retry === 'candidate' && <Link className={linkClass} to="/settings">检查候选索引</Link>}
        {row.retry === 'actor' && row.batch_id && <Link className={linkClass} to={`/batches/${row.batch_id}`}>由原操作人继续</Link>}
        {row.kind === 'archive_broken' && <Link className={linkClass} to="/archive/repairs">修复申请列表</Link>}</td>
    </tr>)}</Table>{errors.data?.length === 0 && <Quiet>暂无待处理异常。</Quiet>}
    {retry.error && <p role="alert" className="text-red-800">{message(retry.error)}</p>}
  </Load>
}

function ParametersForm({ initial }: { initial: SystemParameters }) {
  const [value, setValue] = useState(initial)
  const cache = useQueryClient()
  const save = useMutation({ mutationFn: saveSystem, onSuccess: result => { setValue(result); cache.setQueryData(['maintenance', 'system'], result) } })
  const counters = { session_hours: '登录会话小时数', chunk_mb: '分片大小（MiB）', parallel_files: '上传并行文件数', overdue_days: '审核超期工作日', cooling_days: '物理删除冷静期（天）' } as const
  return <form className="grid grid-cols-3 gap-4" onSubmit={event => { event.preventDefault(); save.mutate(value) }}>
    {(Object.keys(counters) as Array<keyof typeof counters>).map(key => <label className="text-sm" key={key}>{counters[key]}
      <input className="field mt-1" min={key === 'cooling_days' ? 30 : 1} required type="number" value={value[key]}
        onChange={event => setValue({ ...value, [key]: Number(event.target.value) })} /></label>)}
    <label className="text-sm">每日巡检时间<input className="field mt-1" required type="time" value={value.inspection_time}
      onChange={event => setValue({ ...value, inspection_time: event.target.value })} /></label>
    <label className="text-sm">每日备份时间<input className="field mt-1" required type="time" value={value.backup_time}
      onChange={event => setValue({ ...value, backup_time: event.target.value })} /></label>
    <div className="col-span-3 flex items-center gap-4"><span className="text-sm">中国标准时间（Asia/Shanghai，UTC+08:00）</span>
      <GlassButton disabled={save.isPending} type="submit">保存系统参数</GlassButton>
      {save.isSuccess && <span role="status">已保存</span>}</div>
    {save.error && <p role="alert" className="col-span-3 text-red-800">{message(save.error)}</p>}
  </form>
}
function Parameters() {
  const data = useQuery({ queryKey: ['maintenance', 'system'], queryFn: getSystem })
  return <Load loading={data.isPending} error={data.error}>{data.data && <ParametersForm initial={data.data} />}</Load>
}
function Tools() {
  const [ids, setIds] = useState('')
  const submit = useMutation({ mutationFn: () => rebuildThumbs(ids.split(/[、，,\s]+/).filter(Boolean).map(Number)) })
  return <div className="space-y-4">
    <form className="flex items-end gap-3" onSubmit={event => { event.preventDefault(); submit.mutate() }}>
      <label className="text-sm">商品 ID（可填写多个）<input className="field mt-1" required value={ids} onChange={event => setIds(event.target.value)} /></label>
      <GlassButton type="submit" disabled={submit.isPending}>重建缩略图</GlassButton>
    </form>
    {submit.isSuccess && <p role="status">正在使用本次登录的 NAS 权限重建缓存。</p>}
    {submit.error && <p role="alert" className="text-red-800">{message(submit.error)}</p>}
    <Link className={linkClass} to="/settings">模型与蓝绿向量索引管理</Link>
  </div>
}

export function MaintenancePage() {
  const [params, setParams] = useSearchParams()
  const key = params.get('tab') ?? 'inspection'
  const selected = Object.hasOwn(tabs, key) ? key as keyof typeof tabs : 'inspection'
  return <GlassPanel><div className="space-y-5">
    <AdminLinks /><h1 className="text-2xl font-semibold">运营管理</h1>
    <nav aria-label="维护分类" className="flex flex-wrap gap-2">{(Object.keys(tabs) as Array<keyof typeof tabs>).map(tab =>
      <GlassButton key={tab} onClick={() => setParams({ tab })} primary={selected === tab}>{tabs[tab]}</GlassButton>)}</nav>
    {selected === 'inspection' && <Inspection />}
    {selected === 'backup' && <Backups />}
    {selected === 'cleanup' && <Cleanup />}
    {selected === 'errors' && <Errors />}
    {selected === 'logs' && <Logs key="ops" search={false} />}
    {selected === 'search' && <Logs key="search" search />}
    {selected === 'settings' && <Parameters />}
    {selected === 'tools' && <Tools />}
  </div></GlassPanel>
}

export function ArchiveRepairsPage() {
  const cache = useQueryClient()
  const list = useQuery({ queryKey: ['archive-repairs'], queryFn: getRepairs, retry: false })
  const create = useMutation({ mutationFn: createRepairs, onSuccess: () => { void cache.invalidateQueries({ queryKey: ['archive-repairs'] }) } })
  return <GlassPanel><div className="space-y-4">
    <h1 className="text-2xl font-semibold">档案修复申请</h1>
    <Quiet>为项目创建独立草稿。确认修复清单后提交初审，终审通过后由本轮终审本人执行。</Quiet>
    <GlassButton disabled={create.isPending || !list.data?.some(row => row.ready)} onClick={() => create.mutate([])}>为全部可申请项目创建草稿</GlassButton>
    <Load loading={list.isPending} error={list.error}>
      <Table columns={['项目', '原因', '操作']}>{list.data?.map(row => <tr key={row.project_id}><td>{row.project_code} · {row.project_name}</td><td>{row.detail}</td>
        <td><GlassButton disabled={!row.ready || create.isPending} onClick={() => create.mutate([row.project_id])}>{row.ready ? '创建修复草稿' : '项目已有活动批次'}</GlassButton></td></tr>)}</Table>
      {list.data?.length === 0 && <Quiet>暂无可访问的待修复项目。</Quiet>}
    </Load>
    {create.data?.map(row => <p className="text-sm" key={row.project_id}>{row.error ?? <Link className={linkClass} to={`/batches/${row.batch_id}/operation`}>打开修复草稿 #{row.batch_id}</Link>}</p>)}
    {create.error && <p role="alert" className="text-red-800">{message(create.error)}</p>}
  </div></GlassPanel>
}
