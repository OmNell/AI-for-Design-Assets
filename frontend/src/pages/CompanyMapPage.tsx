import { Button, Panel } from '../components/ui'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getCompanyMap, saveCompanyMap, type Company, type CompanyMap, type Industry } from '../api/audit'
import { AdminLinks } from '../components/AdminLinks'

const two = (value: number | null | undefined) => value ? String(value).padStart(2, '0') : '新'

export function CompanyMapPage() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['company-map'], queryFn: getCompanyMap, retry: false })
  const [industries, setIndustries] = useState<Industry[]>([])
  const [companies, setCompanies] = useState<(Company & { aliasText: string })[]>([])
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<{ error?: string; ok?: string }>({})
  const load = (data: CompanyMap) => { setIndustries(data.industries); setCompanies(data.companies.map(item => ({ ...item, aliasText: item.aliases.join('、') }))) }
  useEffect(() => { if (query.data) load(query.data) }, [query.data])
  const save = async () => {
    setBusy(true); setStatus({})
    try {
      // 新行业还没有编号，企业用行业名称关联；已有行业用编号，改名不会断开。
      const names = new Map(industries.map(item => [item.id, item.name.trim()]))
      const saved = await saveCompanyMap({ industries: industries.map(item => ({ ...(item.id ? { id: item.id } : {}), name: item.name.trim() })),
        companies: companies.map(item => ({ ...(item.id ? { id: item.id } : {}), name: item.name.trim(), aliases: item.aliasText.split(/[、，,]/).map(value => value.trim()).filter(Boolean),
          ...(item.industry_id ? { industry_name: names.get(item.industry_id) } : { industry_name: (item.industry_name ?? '').trim() }) })) })
      client.setQueryData(['company-map'], saved); load(saved)
      void client.invalidateQueries({ queryKey: ['catalog-tree'] }); void client.invalidateQueries({ queryKey: ['todos'] })
      setStatus({ ok: '对照表已保存' })
    } catch (reason: unknown) { setStatus({ error: reason instanceof Error ? reason.message : '保存失败，请重试' }) } finally { setBusy(false) }
  }
  return <Panel><AdminLinks /><h1 className="text-2xl font-semibold">企业→行业对照表</h1>
    <p className="mb-6 mt-3 text-sm text-slate-500">按线下商定的结论录入。序号由系统发放，只增不重排；这里的内容决定存放位置的推荐和终审可选的目录树，不会改动 NAS 上的文件夹。</p>
    {query.isPending ? <p>正在加载对照表…</p> : query.isError ? <p role="alert" className="text-red-800">{query.error.message}</p> : <>
      <h2 className="text-lg font-semibold">行业</h2>
      <ul className="mt-3 space-y-2">{industries.map((item, index) => <li key={item.id ?? `new-${index}`} className="flex items-center gap-3">
        <span className="w-10 text-sm tabular-nums text-slate-500">{two(item.seq)}</span>
        <input aria-label={`行业 ${index + 1} 名称`} className="field max-w-xs !py-2 text-sm" value={item.name} onChange={event => setIndustries(industries.map((value, position) => position === index ? { ...value, name: event.target.value } : value))} /></li>)}</ul>
      <div className="mt-3"><Button primary={false} onClick={() => setIndustries([...industries, { name: '' }])}>新增行业</Button></div>
      <h2 className="mt-8 text-lg font-semibold">企业</h2>
      <div className="ui-table-scroll mt-3"><table className="w-full min-w-[28rem] text-left text-sm"><thead className="text-xs text-slate-500"><tr><th className="p-2">序号</th><th className="p-2">标准名（2～10 字）</th><th className="p-2">别名（顿号分隔）</th><th className="p-2">所属行业</th></tr></thead>
        <tbody>{companies.map((item, index) => {
          const update = (patch: Partial<Company & { aliasText: string }>) => setCompanies(companies.map((value, position) => position === index ? { ...value, ...patch } : value))
          return <tr key={item.id ?? `new-${index}`}><td className="p-2 tabular-nums text-slate-500">{two(item.seq)}</td>
            <td className="p-2"><input aria-label={`企业 ${index + 1} 标准名`} className="field !py-2 text-sm" maxLength={10} value={item.name} onChange={event => update({ name: event.target.value })} /></td>
            <td className="p-2"><input aria-label={`企业 ${index + 1} 别名`} className="field !py-2 text-sm" value={item.aliasText} onChange={event => update({ aliasText: event.target.value })} /></td>
            <td className="p-2"><select aria-label={`企业 ${index + 1} 所属行业`} className="field !py-2 text-sm" value={item.industry_id ?? item.industry_name ?? ''}
              onChange={event => { const chosen = industries.find(value => String(value.id ?? value.name) === event.target.value); update({ industry_id: chosen?.id ?? null, industry_name: chosen?.name ?? '' }) }}>
              <option value="">选择行业</option>{industries.filter(value => value.name.trim()).map(value => <option key={value.id ?? value.name} value={value.id ?? value.name}>{value.name}</option>)}</select></td></tr>
        })}</tbody></table></div>
      <div className="mt-3"><Button primary={false} onClick={() => setCompanies([...companies, { name: '', aliases: [], aliasText: '', industry_name: '' }])}>新增企业</Button></div>
      <div className="mt-8 flex flex-wrap items-center gap-4"><Button disabled={busy} onClick={() => void save()}>保存对照表</Button>
        {status.ok && <p role="status" className="text-sm text-emerald-800">{status.ok}</p>}{status.error && <p role="alert" className="text-sm text-red-800">{status.error}</p>}</div></>}
  </Panel>
}
