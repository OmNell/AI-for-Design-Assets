import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getCatalogTree } from '../../api/audit'
import { GlassButton } from '../glass/GlassButton'

/** 终审修改存放位置：中标状态、行业、企业都只能从对照表生成的目录树里选。 */
export function PathPicker({ won, busy, onSave, onCancel }: { won: boolean; busy: boolean; onSave: (won: boolean, companyId: number) => void; onCancel: () => void }) {
  const tree = useQuery({ queryKey: ['catalog-tree'], queryFn: getCatalogTree, retry: false })
  const [chosenWon, setChosenWon] = useState(won)
  const [industry, setIndustry] = useState<number | ''>('')
  const [company, setCompany] = useState<number | ''>('')
  if (tree.isPending) return <p className="mt-2 text-xs text-slate-500">正在加载目录树…</p>
  if (tree.isError) return <p role="alert" className="mt-2 text-xs text-red-800">{tree.error.message}</p>
  const root = tree.data.find(item => item.won === chosenWon)
  const industries = root?.industries ?? []
  const companies = industries.find(item => item.id === industry)?.companies ?? []
  return <div data-testid="path-picker" className="mt-2 space-y-2 rounded-xl bg-white/40 p-3 text-sm">
    <div className="flex gap-4">{tree.data.map(item => <label key={item.folder} className="flex items-center gap-1.5"><input type="radio" name="path-won" checked={chosenWon === item.won} onChange={() => setChosenWon(item.won)} />{item.folder}</label>)}</div>
    <select aria-label="行业" className="field !rounded-xl !px-3 !py-2 text-sm" value={industry} onChange={event => { setIndustry(event.target.value ? Number(event.target.value) : ''); setCompany('') }}>
      <option value="">选择行业</option>{industries.map(item => <option key={item.id} value={item.id}>{item.folder}</option>)}</select>
    <select aria-label="企业" className="field !rounded-xl !px-3 !py-2 text-sm" value={company} disabled={industry === ''} onChange={event => setCompany(event.target.value ? Number(event.target.value) : '')}>
      <option value="">选择企业</option>{companies.map(item => <option key={item.id} value={item.id}>{item.folder}</option>)}</select>
    {!industries.length && <p className="text-xs text-amber-900">目录树还是空的，需要管理员先在对照表中录入行业和企业。</p>}
    <div className="flex gap-2"><GlassButton disabled={busy || company === ''} onClick={() => { if (company !== '') onSave(chosenWon, company) }}>保存位置</GlassButton>
      <GlassButton primary={false} onClick={onCancel}>取消</GlassButton></div>
  </div>
}
