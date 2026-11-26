import { useState } from 'react'
import { Link } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getThumbnailStatus, rebuildThumbs } from '../api/maintenance'
import { changeIndex, getIndexes } from '../api/search'
import {
  getRecovery, getRecoveryReport, runRecovery, type RecoveryBackup, type RecoveryDifference,
  type RecoveryItem, type RecoveryReport, type RecoveryRequest, type RecoveryRun,
} from '../api/recovery'
import { AdminLinks } from '../components/AdminLinks'
import { Badge, Button, EmptyState, ErrorState, Input, LoadingState, Panel, Table } from '../components/ui'
import { formatBytes } from '../lib/terms'

const actions = { detect: '检测资产状态', scan: '扫描 NAS 已入库资产', apply: '执行档案恢复', stage: '核验并暂存数据库备份' }
const catalogStates: Record<string, string> = {
  local_present: '本地已有正式资产', needs_scan: '检测到可能缺失的正式资产',
  empty_confirmed: '本地为空，NAS 初步检查未发现正式档案', empty_unconfirmed: '本地尚未恢复历史资产',
}
const nasStates: Record<string, string> = {
  not_checked: '未检测', sampled: '已抽查发布凭据', empty: '初步检查未发现正式档案',
  unconfirmed: '资产状态暂时无法确认', nas_unavailable: 'NAS 暂时无法访问',
  system_access_required: '请配置可读取正式区和备份区的系统账号', formal_root_missing: '未找到 NAS 正式区',
}
const taskStates: Record<string, string> = {
  queued: '已排队', running: '正在执行', done: '已完成', partial: '部分完成', failed: '失败',
}
const phases: Record<string, string> = {
  detection: '正在检测', detecting: '正在检测', scanning: '正在扫描档案',
  indexing: '正在核对和恢复向量', validating: '正在校验档案和文件', project_complete: '已恢复项目',
  staging: '正在核验和迁移备份', restoring: '正在恢复基础数据', recovering: '正在核验和恢复数据',
}
const itemStates: Record<RecoveryItem['kind'], string> = {
  missing: '本地缺失', incomplete: '部分记录缺失', newer: 'NAS 有较新版本',
  same: '一致', conflict: '待人工确认', local_only: '本轮未找到 NAS 档案',
}
const backupStates: Record<string, string> = {
  invalid: '清单无效', empty: '空库备份，待核对', manifest_checked: '清单已核验', unverified: '待核验',
}
const linkClass = 'text-sm font-medium text-blue-700 underline underline-offset-4'
const active = (run: RecoveryRun) => run.status === 'queued' || run.status === 'running'
const runLabel = (run: RecoveryRun) => run.progress?.phase === 'partial' ? taskStates.partial : taskStates[run.status] ?? run.status
const hasReport = (run?: RecoveryRun) => run?.status === 'done' || run?.status === 'partial'
const canApply = (item: RecoveryItem) => item.kind === 'missing' || item.kind === 'incomplete' || item.kind === 'newer'
const message = (error: Error) => error.message || '操作失败，请稍后重试'
const time = (value?: string | null) => value ? new Date(/Z|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z')
  .toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '未检测'

function Metrics({ values }: { values: [string, React.ReactNode][] }) {
  return <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">{values.map(([name, value]) =>
    <div className="min-w-0 rounded-lg bg-slate-50 p-3" key={name}>
      <dt className="text-xs font-medium text-slate-600">{name}</dt>
      <dd className="mt-1.5 break-words font-semibold tabular-nums text-slate-900">{value ?? '未检测'}</dd>
    </div>)}</dl>
}

function Differences({ report }: { report: RecoveryDifference }) {
  const [showAll, setShowAll] = useState(false)
  const items = report.items ?? []
  const visible = showAll ? items : items.filter(row => row.kind !== 'same')
  return <div className="space-y-3">
    {items.length > visible.length && <Button primary={false} onClick={() => setShowAll(true)}>同时查看一致项目</Button>}
    {showAll && items.some(row => row.kind === 'same') && <Button primary={false} onClick={() => setShowAll(false)}>只看差异</Button>}
    {!!visible.length && <div className="max-h-[28rem] overflow-auto"><Table label="项目差异">
      <thead><tr><th>项目编号</th><th>正式版本</th><th>状态与原因</th><th>档案目录</th></tr></thead>
      <tbody>{visible.map(row => <tr key={row.project_id}>
        <td className="whitespace-nowrap">{row.code}</td><td>{row.version ?? '—'}</td>
        <td><span className="font-medium">{itemStates[row.kind]}</span><p className="text-slate-600">{row.reason}</p></td>
        <td className="max-w-56 break-all">{row.path}</td>
      </tr>)}</tbody>
    </Table></div>}
    {!!report.unresolved?.length && <details open>
      <summary className="cursor-pointer font-medium">待人工核验的档案（{report.unresolved.length}）</summary>
      <ul className="mt-2 max-h-64 list-inside list-disc space-y-2 overflow-auto text-sm">{report.unresolved.map(row =>
        <li key={row.project_code} className="break-words">{row.project_code} · {row.reason}
          {row.candidates.map(candidate => <p key={candidate.path} className="ml-4 break-all text-xs text-slate-600">
            {candidate.path} · {candidate.error || (candidate.publication_receipt ? '已有凭据，需核对版本' : '缺少有效发布凭据')}
          </p>)}</li>)}</ul>
    </details>}
    {!!report.inaccessible?.length && <details>
      <summary className="cursor-pointer font-medium">无法核验的目录（{report.inaccessible.length}）</summary>
      <ul className="mt-2 max-h-64 list-inside list-disc overflow-auto text-sm">{report.inaccessible.map(row =>
        <li key={row.path} className="break-all">{row.path} · {row.reason}</li>)}</ul>
    </details>}
    {!!report.not_recoverable?.length && <details>
      <summary className="cursor-pointer text-sm text-slate-600">仅凭档案无法恢复的信息</summary>
      <p className="mt-1 text-sm text-slate-600">{report.not_recoverable.join('、')}。</p>
    </details>}
  </div>
}

function BackupList({ backups, state, busy, onStage }: {
  backups?: RecoveryBackup[]; state?: string; busy: boolean; onStage: (backup: RecoveryBackup) => void
}) {
  if (!backups?.length) return <p className="text-sm text-slate-600">{state === 'checked'
    ? '本轮没有发现备份归档。' : '备份状态暂时无法确认，请检查系统账号对 NAS 备份区的读取权限。'}</p>
  const preferred = backups.find(row => row.validation === 'manifest_checked')
  return <Table label="NAS 可恢复备份">
    <thead><tr><th>备份</th><th>时间</th><th>大小</th><th>格式及校验</th><th>操作</th></tr></thead>
    <tbody>{backups.map(row => <tr key={row.name}>
      <td className="break-all">{row.name} {preferred?.name === row.name && <Badge tone="info">优先核验</Badge>}</td><td>{time(row.modified_at)}</td>
      <td className="whitespace-nowrap">{formatBytes(row.size)}</td>
      <td>{row.app_version ? `工作台 ${row.app_version} · ` : row.format ? `格式 ${row.format} · ` : ''}
        {backupStates[row.validation] ?? row.validation}</td>
      <td><Button primary={false} disabled={busy || row.validation === 'invalid' || row.validation === 'empty'}
        onClick={() => onStage(row)}>选择备份</Button></td>
    </tr>)}</tbody>
  </Table>
}

function Outcome({ report }: { report: RecoveryReport }) {
  return <div className="space-y-3">
    {report.stage_dir && <>
      <p className="text-sm">已暂存到 <code className="break-all">{report.stage_dir}</code></p>
      <p className="text-sm text-slate-600">数据库迁移 {report.source_revision} → {report.revision}。
        校验下方差异后，按部署恢复手册停止目标工作台并切换数据目录，保留切换前的配置和数据库用于回滚。</p>
      {report.inventory && <Metrics values={[
        ['项目', report.inventory.projects?.count], ['商品', report.inventory.products?.count],
        ['文件', report.inventory.files?.count], ['短链接', report.inventory.links?.count],
        ['向量', report.inventory.product_embeddings?.count],
      ]} />}
    </>}
    {report.recovered && <p className="text-sm">本次恢复 {report.recovered.length} 个项目，已一致 {report.skipped?.length ?? 0} 个。</p>}
    {!!report.blocked?.length && <ul className="list-inside list-disc space-y-1 text-sm text-amber-900">
      {report.blocked.map(row => <li key={row.project_id}>项目 {row.project_id} · {row.reason}</li>)}
    </ul>}
    <Differences report={report.difference ?? report} />
  </div>
}

function DerivedData({ busy }: { busy: boolean }) {
  const cache = useQueryClient()
  const [price, setPrice] = useState('')
  const indexes = useQuery({ queryKey: ['vector-indexes'], queryFn: getIndexes, refetchInterval: query =>
    query.state.data?.indexes.some(row => row.running) ? 3000 : false })
  const thumbnails = useQuery({ queryKey: ['recovery-thumbnails'], queryFn: getThumbnailStatus, refetchInterval: query =>
    query.state.data?.running ? 3000 : false })
  const formal = indexes.data?.indexes.find(row => row.status === 'formal')
  const tokens = formal?.estimated_input_tokens
  const amount = Number(price)
  const estimate = tokens != null && price !== '' && Number.isFinite(amount) && amount >= 0
    ? new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 6 }).format(tokens * amount / 1_000_000)
    : '待计价'
  const vector = useMutation({
    mutationFn: (productId?: number) => changeIndex(formal!.generation, 'retry', productId),
    onSuccess: data => cache.setQueryData(['vector-indexes'], data),
  })
  const images = useMutation({
    mutationFn: () => rebuildThumbs([], true, true),
    onSuccess: () => { void cache.invalidateQueries({ queryKey: ['recovery-thumbnails'] }) },
  })
  return <Panel className="min-w-0 space-y-3">
    <h2 className="text-lg font-semibold">检索索引与缩略图</h2>
    <Metrics values={[
      ['已校验向量', formal?.ready], ['向量缺失或失效', formal ? formal.pending + formal.failures : null],
      ['缩略图已就绪', thumbnails.data?.ready], ['缺失缩略图', thumbnails.data?.missing],
    ]} />
    {!formal && indexes.isSuccess && <p className="text-sm text-slate-600">尚未建立正式向量索引。档案恢复会使用配置中的向量模型。</p>}
    {formal?.model_issue && <p className="text-sm text-amber-900">{formal.model_issue}</p>}
    {formal && <p className="text-sm text-slate-600">
      {formal.running ? '向量构建中' : `预计为 ${formal.estimated_calls ?? formal.pending + formal.failures} 个商品生成向量。失败重试可能增加调用，费用按服务商实际用量计费。`}
    </p>}
    {formal && (formal.pending + formal.failures > 0) && <div className="flex flex-wrap items-end gap-3 text-sm">
      <label>向量模型单价（元／百万输入词元）
        <Input className="mt-1" type="number" min={0} step="any" value={price} onChange={event => setPrice(event.target.value)} />
      </label>
      <p className="text-slate-600">预计输入词元 {tokens?.toLocaleString('zh-CN') ?? '暂无用量样本'} · 预估费用 {estimate}</p>
    </div>}
    <div className="flex flex-wrap items-center gap-3">
      <Button primary={false} loading={vector.isPending} disabled={busy || !formal || formal.running || !!formal.model_issue || formal.verified}
        onClick={() => vector.mutate(undefined)}>补齐正式检索索引</Button>
      <Button primary={false} loading={images.isPending} disabled={busy || !thumbnails.data?.missing || !!thumbnails.data.running}
        onClick={() => images.mutate()}>重建缺失缩略图</Button>
      <Link className={linkClass} to="/settings">向量模型与候选索引</Link>
    </div>
    {!!formal?.retry_product_ids?.length && <details>
      <summary className="cursor-pointer text-sm">单独重试失败商品</summary>
      <div className="mt-2 flex max-h-32 flex-wrap gap-2 overflow-auto">{formal.retry_product_ids.map(id =>
        <Button key={id} primary={false} disabled={busy || vector.isPending || formal.running || !!formal.model_issue}
          onClick={() => vector.mutate(id)}>商品 {id}</Button>)}</div>
    </details>}
    {images.isSuccess && <p role="status" className="text-sm">已提交本批缺失缩略图。剩余项目可继续分批提交。</p>}
    {(indexes.error || thumbnails.error || vector.error || images.error) &&
      <ErrorState>{message((vector.error ?? images.error ?? indexes.error ?? thumbnails.error)!)}</ErrorState>}
  </Panel>
}

export function AssetRecoveryPage() {
  const cache = useQueryClient()
  const [selection, select] = useState<number>()
  const [confirmation, confirm] = useState<{ action: 'apply'; id: number; count: number } | { action: 'stage'; name: string }>()
  const status = useQuery({ queryKey: ['asset-recovery'], queryFn: getRecovery, refetchInterval: 4000 })
  const current = status.data?.runs.find(run => run.id === selection) ?? status.data?.runs[0]
  const lastDetect = status.data?.runs.find(run => run.action === 'detect' && hasReport(run))
  const detected = useQuery({
    queryKey: ['asset-recovery', 'report', lastDetect?.id], queryFn: () => getRecoveryReport(lastDetect!.id), enabled: !!lastDetect,
  })
  const detail = useQuery({
    queryKey: ['asset-recovery', 'report', current?.id], queryFn: () => getRecoveryReport(current!.id), enabled: hasReport(current),
  })
  const work = useMutation({
    mutationFn: (request: RecoveryRequest) => runRecovery(request),
    onSuccess: run => {
      select(run.id)
      confirm(undefined)
      cache.setQueryData(['asset-recovery'], (old: typeof status.data) => old &&
        ({ ...old, runs: [run, ...old.runs.filter(item => item.id !== run.id)] }))
      void cache.invalidateQueries({ queryKey: ['asset-recovery'] })
    },
  })
  const busy = work.isPending || !!status.data?.runs.some(active)
  const recoverable = detail.data?.items?.filter(canApply).length ?? 0
  const lastApply = status.data?.runs.find(run => run.action === 'apply' && run.finished_at)
  const lastScan = status.data?.runs.find(run => run.action === 'scan' && run.finished_at)
  const state = status.data
  return <div className="space-y-4">
    <Panel className="min-w-0 space-y-4">
      <AdminLinks />
      <h1 className="text-2xl font-semibold">资产恢复与索引管理</h1>
      {status.isPending && <LoadingState>正在读取资产状态…</LoadingState>}
      {status.error && <ErrorState retry={() => void status.refetch()}>{message(status.error)}</ErrorState>}
      {state && <>
        <div className="flex flex-wrap gap-2"><Badge tone={state.catalog_state === 'local_present' ? 'info' : 'warning'}>
          {catalogStates[state.catalog_state] ?? '本地资产状态待确认'}
        </Badge><Badge>{nasStates[state.nas_state] ?? 'NAS 状态未检测'}</Badge>
          {state.read_only && <Badge tone="info">NAS 业务只读</Badge>}</div>
        <Metrics values={[
          ['本地已登记项目', state.projects], ['本地已登记商品', state.products], ['本地已登记文件', state.files],
          ['最近验证的备份', time(state.last_backup_at)], ['最近档案恢复', time(lastApply?.finished_at)],
        ]} />
        <p className="text-sm text-slate-600">最近 NAS 检测 {time(state.nas_checked_at)} · 最近档案核验 {time(lastScan?.finished_at)}
          {lastScan?.summary && ` · 待恢复 ${lastScan.summary.recoverable ?? '未检测'} · 待确认 ${lastScan.summary.unresolved ?? '未检测'}`}
        </p>
        {(state.catalog_state === 'empty_unconfirmed' || state.catalog_state === 'needs_scan') &&
          <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">新环境可先检测 NAS 备份，优先核验最近有效的备份。备份较早或无法使用时，再扫描正式档案补齐历史资产。</p>}
        <div className="flex flex-wrap gap-3">
          <Button disabled={busy} onClick={() => work.mutate({ action: 'detect' })}>{actions.detect}</Button>
          <Button primary={false} disabled={busy} onClick={() => work.mutate({ action: 'scan' })}>{actions.scan}</Button>
          <Link className={linkClass + ' self-center'} to="/admin/nas-settings">设置 NAS 恢复账号</Link>
        </div>
        {detected.isError && <ErrorState retry={() => void detected.refetch()}>{message(detected.error)}</ErrorState>}
        {detected.data && <BackupList backups={detected.data.backups} state={detected.data.backup_state} busy={busy}
          onStage={backup => confirm({ action: 'stage', name: backup.name })} />}
      </>}
    </Panel>
    {state && <Panel className="min-w-0 space-y-4">
      <h2 className="text-lg font-semibold">恢复计划与运行记录</h2>
      {!state.runs.length && <EmptyState title="尚无恢复记录">检测资产状态或扫描 NAS 后查看本次结果。</EmptyState>}
      {!!state.runs.length && <Table label="恢复记录">
        <thead><tr><th>操作</th><th>操作者</th><th>开始时间</th><th>状态</th><th>报告</th></tr></thead>
        <tbody>{state.runs.map(run => <tr key={run.id}>
          <td>{actions[run.action] ?? run.action}</td><td>{run.actor ?? '系统'}</td><td>{time(run.started_at)}</td>
          <td><Badge tone={run.status === 'failed' ? 'danger' : active(run) ? 'info' : 'neutral'}>{runLabel(run)}</Badge></td>
          <td><button className={linkClass} aria-pressed={run.id === current?.id} onClick={() => { select(run.id); confirm(undefined) }}>查看第 {run.id} 次记录</button></td>
        </tr>)}</tbody>
      </Table>}
      {current && <section className="space-y-3 rounded-lg border border-slate-200 p-4" aria-label="恢复任务详情">
        <h3 className="font-semibold">{actions[current.action]} · {runLabel(current)}</h3>
        {active(current) && <p role="status" className="text-sm">
          {phases[current.progress?.phase ?? ''] ?? '等待后台更新进度'}
          {current.progress?.code && ` · ${current.progress.code}`}
          {current.progress?.products != null && ` · ${current.progress.products} 个商品`}
        </p>}
        {current.error && <ErrorState>{current.error}</ErrorState>}
        {hasReport(current) && detail.isPending && <LoadingState>正在读取报告…</LoadingState>}
        {hasReport(current) && detail.error && <ErrorState retry={() => void detail.refetch()}>{message(detail.error)}</ErrorState>}
        {detail.data && current.action !== 'detect' && <Outcome key={current.id} report={detail.data} />}
        {detail.data && current.action === 'detect' && <p className="text-sm">备份候选 {detail.data.backups?.length ?? '未检测'} 个。</p>}
        {current.action === 'scan' && recoverable > 0 && <Button disabled={busy}
          onClick={() => confirm({ action: 'apply', id: current.id, count: recoverable })}>执行 {recoverable} 个项目的恢复计划</Button>}
      </section>}
    </Panel>}
    {confirmation && <Panel className="space-y-3 border-amber-200 bg-amber-50">
      <h2 className="text-lg font-semibold">{confirmation.action === 'apply' ? '确认恢复项目' : '确认核验并暂存备份'}</h2>
      <p className="break-words text-sm">{confirmation.action === 'apply'
        ? `核验正式发布凭据、档案、文件和必要向量后，按项目更新本地卡片柜。计划包含 ${confirmation.count} 个项目。替代原环境时，请先停止旧工作台对同一 NAS 的业务写入。`
        : `备份 ${confirmation.name} 将解包到隔离目录，查验校验值与原密钥，并检查数据库迁移。正式切换由运维在停机后完成。`}</p>
      <div className="flex flex-wrap gap-3">
        <Button loading={work.isPending} disabled={busy} onClick={() => work.mutate(confirmation.action === 'apply'
          ? { action: 'apply', plan_id: confirmation.id, confirmed: true }
          : { action: 'stage', backup_name: confirmation.name, confirmed: true })}>确认执行</Button>
        <Button primary={false} onClick={() => confirm(undefined)}>取消</Button>
      </div>
    </Panel>}
    {work.error && <ErrorState>{message(work.error)}</ErrorState>}
    {state && <DerivedData busy={busy} />}
  </div>
}
