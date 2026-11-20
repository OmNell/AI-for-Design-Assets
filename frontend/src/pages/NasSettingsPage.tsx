import { useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { applyNas, browseNas, getNasConfig, getNasStatus, testNas, validateNas, type NasCandidate, type NasCheck, type NasPublic, type NasStatus, type NasValidation } from '../api/nasSettings'
import { ApiError } from '../api/client'
import { AdminLinks } from '../components/AdminLinks'
import { Badge, Breadcrumb, Button, Dialog, EmptyState, ErrorState, FileIcon, Icon, Input, LoadingState, Panel, Select } from '../components/ui'
import { useAuthStore } from '../stores/auth'

const names = { formal: '设计资产', temp: '工作台临时区', backup: '_工作台备份' }
const stages = { active: '已启用', pending_restart: '待重启', failed: '应用失败' } as const
const messages = { failed: '失败', manual: '待现场验证', passed: '通过' } as const

function Checks({ rows }: { rows: NasCheck[] }) {
  return <ul className="divide-y divide-slate-100">{rows.map(row => <li className="py-2" key={row.name}>
    <div className="flex items-start justify-between gap-2"><p className="font-medium">{row.name}</p>
      <Badge tone={row.state === 'failed' ? 'danger' : row.state === 'manual' ? 'warning' : 'success'}>{messages[row.state]}</Badge></div>
    <p className="mt-1 break-words text-xs text-slate-500">{row.message}</p>
  </li>)}</ul>
}

function Addresses({ config }: { config: NasPublic }) {
  return <dl className="space-y-1 break-words text-xs text-slate-600">
    <dt className="sr-only">服务器</dt><dd>{config.host}:{config.dsm_port} · {config.share}</dd>
    <dt className="sr-only">工作目录</dt><dd>{config.work_root ? `/${config.work_root}` : '沿用 YAML 的三个独立区域'}</dd>
  </dl>
}

const describe = (error: unknown) => error instanceof Error ? error.message : '请求失败，请稍后重试'

function Workspace({ initial }: { initial: NasStatus }) {
  const { active } = initial
  const account = useAuthStore(state => state.user?.username)
  const [status, setStatus] = useState(initial)
  const [target, setTarget] = useState({ host: active.host, dsm_port: String(active.dsm_port), share: active.share })
  const [workRoot, setWorkRoot] = useState(active.work_root ?? '')
  const [username, setUsername] = useState(active.system_user)
  const [systemPassword, setSystemPassword] = useState('')
  const [actorPassword, setActorPassword] = useState('')
  const [testUser, setTestUser] = useState('')
  const [testPassword, setTestPassword] = useState('')
  const [profile, setProfile] = useState<'employee' | 'final'>('employee')
  const [testChecks, setTestChecks] = useState<NasCheck[]>([])
  const [result, setResult] = useState<NasValidation>()
  const [nodes, setNodes] = useState<{ path: string; nodes: string[]; truncated: boolean }>()
  const [path, setPath] = useState('')
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const locked = status.state === 'pending_restart'
  const port = Number(target.dsm_port)
  const targetReady = target.host.trim() !== '' && target.share.trim() !== '' && Number.isInteger(port) && port > 0 && port <= 65535
  const canConnect = targetReady && !!actorPassword && !locked
  const canCheck = canConnect && !!workRoot && !!username
  const server = { host: target.host.trim(), dsm_port: port, share: target.share.trim() }
  const candidate: NasCandidate = { ...server, work_root: workRoot, system_user: username.trim(), actor_password: actorPassword,
    ...(systemPassword ? { system_password: systemPassword } : {}) }
  const clearChecks = (connection = false) => {
    setResult(undefined)
    if (connection) { setNodes(undefined); setPath(''); setTestChecks([]) }
  }
  async function perform(label: string, callback: () => Promise<void>) {
    if (busy) return
    setBusy(label)
    setError('')
    try { await callback() }
    catch (reason) {
      if (reason instanceof ApiError && reason.code === 'nas_configuration_blocked' && reason.detail &&
          typeof reason.detail === 'object' && 'eligible' in reason.detail) setResult(reason.detail as NasValidation)
      setError(describe(reason))
    } finally { setBusy('') }
  }
  const fetchNodes = (next: string) => void perform('browse', async () => {
    setNodes(undefined)
    setPath(next)
    setNodes(await browseNas(server, next, actorPassword))
  })
  const runTest = (protocol: 'dsm' | 'smb') => void perform(protocol, async () =>
    setTestChecks(await testNas({ ...server, protocol, actor_password: actorPassword })))
  const relatives = path ? path.split('/') : []
  const refresh = () => void perform('status', async () => {
    const fresh = await getNasStatus()
    setStatus(fresh)
    if (fresh.revision !== status.revision) clearChecks(true)
  })
  const confirm = () => void perform('apply', async () => {
    const next = await applyNas(candidate, status.revision!)
    setActorPassword(''); setSystemPassword(''); setTestPassword('')
    setStatus(next); setResult(undefined); setNodes(undefined); setOpen(false)
  })
  return <>
    <AdminLinks />
    <h1 className="mb-4 text-2xl font-semibold">NAS 存储设置</h1>
    {error && <div className="mb-4"><ErrorState>{error}</ErrorState></div>}
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_320px] 2xl:grid-cols-[minmax(0,1fr)_340px]">
      <div className="min-w-0 space-y-4">
        <Panel>
          <h2 className="mb-4 font-semibold">NAS 服务器</h2>
          <fieldset disabled={!!busy || locked} className="grid min-w-0 gap-4 md:grid-cols-2">
            <label>NAS IP 或内网域名<Input className="mt-1" maxLength={253} value={target.host} onChange={e => { setTarget({ ...target, host: e.target.value }); clearChecks(true) }} /></label>
            <label>DSM HTTPS 端口<Input type="number" className="mt-1" min={1} max={65535} value={target.dsm_port} onChange={e => { setTarget({ ...target, dsm_port: e.target.value }); clearChecks(true) }} /></label>
            <label>SMB 共享文件夹<Input className="mt-1" maxLength={80} value={target.share} onChange={e => { setTarget({ ...target, share: e.target.value }); clearChecks(true) }} /></label>
            <div><p>DSM 证书验证</p><p className="mt-2 text-xs text-slate-600">{active.certificate === 'mac_test_disabled' ? 'HTTPS 已启用，证书验证关闭（本机测试）' :
              active.certificate === 'trusted_ca' ? '使用服务器配置的专用 CA' :
              active.certificate === 'legacy_http' ? '旧配置使用 HTTP，待保存的候选强制使用 HTTPS 验证' : '使用系统受信任 CA'}</p></div>
            <label>系统账号名称<Input className="mt-1" value={username} maxLength={128} onChange={e => { setUsername(e.target.value); clearChecks() }} /></label>
            <label>更新系统账号密码<Input className="mt-1" type="password" autoComplete="new-password" maxLength={512} placeholder={active.has_system_password ? '留空保留现有密码' : '请输入系统账号密码'} value={systemPassword} onChange={e => { setSystemPassword(e.target.value); clearChecks() }} /></label>
            <label className="md:col-span-2">当前管理员 NAS 密码（{account}）<Input className="mt-1" type="password" autoComplete="off" maxLength={512} value={actorPassword} onChange={e => { setActorPassword(e.target.value); clearChecks(true) }} /></label>
            <div className="flex flex-wrap gap-2 md:col-span-2">
              <Button primary={false} loading={busy === 'dsm'} disabled={!canConnect} onClick={() => runTest('dsm')}>测试 DSM</Button>
              <Button primary={false} loading={busy === 'smb'} disabled={!canConnect} onClick={() => runTest('smb')}>测试 SMB</Button>
            </div>
          </fieldset>
          {testChecks.length > 0 && <Checks rows={testChecks} />}
        </Panel>
        <Panel>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">指定工作目录</h2>
            {active.work_root && <Badge tone="info">已绑定 /{active.work_root}</Badge>}</div>
          <label>共享内相对路径<Input className="mt-1" placeholder="例如 部门/设计工作台" value={workRoot} maxLength={768} disabled={!!busy || locked}
            onChange={e => { setWorkRoot(e.target.value); clearChecks() }} /></label>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button primary={false} disabled={!!busy || !canConnect} onClick={() => fetchNodes('')}>浏览共享</Button>
            <Button primary={false} disabled={!!busy || !canConnect} onClick={() => fetchNodes(workRoot)}>验证并打开路径</Button>
          </div>
          {(nodes || busy === 'browse') && <div className="mt-4 rounded-lg border border-slate-200">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 p-3">
              <Breadcrumb><button type="button" className="ui-link" disabled={!!busy} onClick={() => fetchNodes('')}>共享</button>
                {relatives.map((name, index) => <span key={index} className="flex min-w-0 items-center gap-1"><Icon name="right" /><button className="ui-link break-all" disabled={!!busy} type="button" onClick={() => fetchNodes(relatives.slice(0, index + 1).join('/'))}>{name}</button></span>)}
              </Breadcrumb>
              <div className="flex flex-wrap gap-2"><Button primary={false} disabled={!!busy} onClick={() => fetchNodes(path)}>刷新</Button>
                <Button disabled={!!busy || !path || !nodes} onClick={() => { setWorkRoot(path); clearChecks() }}>选择当前目录</Button>
                <Button primary={false} disabled={!!busy} onClick={() => setNodes(undefined)}>折叠</Button></div>
            </div>
            <p className="break-all border-b border-slate-100 px-3 py-1 text-xs text-slate-500">/{path}</p>
            {busy === 'browse' ? <LoadingState>正在读取授权目录…</LoadingState> : nodes?.nodes.length ?
              <ul className="max-h-72 divide-y divide-slate-100 overflow-auto">{nodes.nodes.map(name => <li key={name}>
                <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-blue-50" onClick={() => fetchNodes(path ? `${path}/${name}` : name)}><FileIcon folder /><span className="break-all">{name}</span><Icon name="right" className="ml-auto text-slate-400" /></button>
              </li>)}</ul> : <EmptyState title="此目录没有可浏览的子文件夹" />}
            {nodes?.truncated && <p className="p-2 text-xs text-amber-800">仅显示部分目录。也可输入相对路径并验证。</p>}
          </div>}
        </Panel>
        <Panel>
          <h2 className="mb-3 font-semibold">配置预览与影响</h2>
          <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-2 break-all text-sm">
            <dt>目标共享</dt><dd>{server.host || '—'} / {server.share || '—'}</dd>
            <dt>工作目录</dt><dd>{workRoot ? `/${workRoot}` : '尚未选择'}</dd>
            {Object.entries(names).map(([key, name]) => <div key={key} className="contents"><dt>{name}</dt><dd>{result?.regions[key] ?? (workRoot ? `/${workRoot}/${name}` : '—')}</dd></div>)}
          </dl>
          <p className="mt-3 text-xs text-slate-500">三个区域由 NAS 管理员预建并配置 ACL。保存后由运维重启工作台。</p>
          {result && <div className="my-3 space-y-2" aria-live="polite">
            <Badge tone={result.eligible ? 'success' : 'danger'}>{result.eligible ? '关键检查通过，可保存' : '配置不能启用'}</Badge>
            {result.changes.length > 0 && <p className="text-xs">将变更 {result.changes.join('、')}</p>}
            {result.conflicts.length > 0 && <ErrorState><ul>{result.conflicts.map(message => <li key={message}>{message}</li>)}</ul></ErrorState>}
            <Checks rows={result.checks} />
          </div>}
          <div className="mt-4 flex flex-wrap gap-2">
            <Button primary={false} loading={busy === 'validate'} disabled={!!busy || !canCheck} onClick={() => void perform('validate', async () => setResult(await validateNas(candidate)))}>检查配置</Button>
            <Button disabled={!!busy || locked || !result?.eligible || !status.revision} onClick={() => setOpen(true)}>保存配置</Button>
          </div>
        </Panel>
      </div>
      <div className="min-w-0 space-y-4">
        <Panel><div className="mb-3 flex items-center justify-between gap-2"><h2 className="font-semibold">运行状态</h2>
          <Badge tone={status.state === 'active' ? 'success' : status.state === 'failed' ? 'danger' : 'warning'}>{stages[status.state]}</Badge></div>
          <p className="mb-3 text-sm">{status.message}</p>
          {status.mode === 'dev' && <p className="mb-3 text-xs text-slate-500">开发模式使用模拟 NAS。</p>}
          <h3 className="mb-1 font-medium">当前运行配置</h3><Addresses config={status.active} />
          {status.pending && <div className="mt-4 border-t border-slate-200 pt-3"><h3 className="mb-1 font-medium">重启后生效</h3><Addresses config={status.pending} /></div>}
          {!!status.connection_checks.length && <Checks rows={status.connection_checks} />}
          {!locked && !status.connection_checks.length && <p className="mt-3 text-xs text-slate-500">本次页面尚未复检连接。</p>}
          <Button className="mt-3" primary={false} disabled={!!busy} loading={busy === 'status'} onClick={refresh}>刷新状态</Button>
        </Panel>
        <Panel><details><summary className="cursor-pointer font-semibold">检查授权测试账号</summary>
          <form className="mt-3 space-y-3" onSubmit={(e: FormEvent) => { e.preventDefault(); void perform('profile', async () => {
            try { setTestChecks(await testNas({ ...server, protocol: 'smb', username: testUser.trim(), actor_password: testPassword, permission_profile: profile, work_root: workRoot })) }
            finally { setTestPassword('') }
          }) }}>
            <label>角色<Select className="mt-1" value={profile} onChange={e => setProfile(e.target.value as typeof profile)}><option value="employee">普通员工</option><option value="final">终审</option></Select></label>
            <label className="block">测试账号<Input className="mt-1" maxLength={128} value={testUser} onChange={e => setTestUser(e.target.value)} /></label>
            <label className="block">测试账号密码<Input className="mt-1" maxLength={512} type="password" autoComplete="off" value={testPassword} onChange={e => setTestPassword(e.target.value)} /></label>
            <Button type="submit" primary={false} loading={busy === 'profile'} disabled={!!busy || !targetReady || !workRoot || !testUser.trim() || !testPassword || locked}>检查人员 ACL</Button>
          </form>
        </details></Panel>
      </div>
    </div>
    <Dialog title="确认保存 NAS 配置" open={open} onClose={() => { if (!busy) setOpen(false) }}>
      <p>保存后会暂停业务访问，直到运维重启并完成连接复检。复检失败会恢复原配置。</p>
      <p className="mt-2 break-all text-slate-600">{server.host} / {server.share} / {workRoot}</p>
      <Button className="mt-4" loading={busy === 'apply'} disabled={!!busy} onClick={confirm}>确认保存并待重启</Button>
    </Dialog>
  </>
}

export function NasSettingsPage() {
  const data = useQuery({ queryKey: ['nas-settings'], queryFn: getNasConfig, staleTime: 0 })
  return data.isPending ? <LoadingState /> : data.isError ? <ErrorState retry={() => void data.refetch()}>{data.error.message}</ErrorState> :
    <Workspace key={data.data.revision} initial={data.data} />
}
