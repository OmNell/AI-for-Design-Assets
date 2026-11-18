import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ReviewField } from '../../api/review'
import { listUsers } from '../../api/roles'
import { GlassButton } from '../glass/GlassButton'
import { GlassModal } from '../glass/GlassModal'

interface Common { onClose: () => void; busy: boolean; error: string }
const Failure = ({ error }: { error: string }) => error ? <p role="alert" className="mt-4 text-sm text-red-800">{error}</p> : null

export function BulkModal({ fields, count, onApply, onClose, busy, error }: Common & { fields: ReviewField[]; count: number; onApply: (tags: Record<string, string>) => void }) {
  const [chosen, setChosen] = useState<Record<string, string>>({})
  const keys = Object.keys(chosen)
  return <GlassModal open title={`批量修改 · 已选 ${count} 张`} onClose={onClose}>
    <p className="text-sm text-slate-500">勾选要统一设置的字段并填写内容，所选图片的该字段会整体替换为同一段文字。</p>
    <div className="mt-4 max-h-[50vh] space-y-2 overflow-y-auto pr-1">{fields.map(field => <div key={field.key} className="flex items-center gap-3">
      <label className="flex w-28 shrink-0 items-center gap-2 text-sm"><input type="checkbox" checked={field.key in chosen}
        onChange={event => setChosen(current => { const next = { ...current }; if (event.target.checked) next[field.key] = ''; else delete next[field.key]; return next })} />{field.label}</label>
      <input aria-label={`批量设置${field.label}`} className="field !rounded-xl !px-3 !py-2 text-sm disabled:opacity-50" maxLength={500} disabled={!(field.key in chosen)}
        placeholder={field.hint} value={chosen[field.key] ?? ''} onChange={event => setChosen(current => ({ ...current, [field.key]: event.target.value }))} /></div>)}</div>
    <Failure error={error} />
    <div className="mt-5"><GlassButton disabled={busy || !keys.length} onClick={() => onApply(chosen)}>应用到 {count} 张图</GlassButton></div>
  </GlassModal>
}

export function AppendModal({ onAppend, onClose, busy, error }: Common & { onAppend: (kind: 'set' | 'item', file: File, name: string) => void }) {
  const [kind, setKind] = useState<'set' | 'item'>('item')
  const [file, setFile] = useState<File | null>(null)
  const [name, setName] = useState('')
  const valid = !!file && /^[^/\\]+\.(jpg|jpeg|png)$/i.test(name.trim())
  return <GlassModal open title="追加主图" onClose={onClose}>
    <div className="flex gap-6 text-sm">{(['item', 'set'] as const).map(value => <label key={value} className="flex items-center gap-2">
      <input type="radio" name="append-kind" checked={kind === value} onChange={() => setKind(value)} />{value === 'item' ? '放入 单品/' : '放入 套装/'}</label>)}</div>
    <input type="file" aria-label="选择图片" accept=".jpg,.jpeg,.png,image/jpeg,image/png" className="mt-4 block w-full text-sm"
      onChange={event => { const picked = event.target.files?.[0] ?? null; setFile(picked); setName(picked?.name ?? '') }} />
    <label className="mt-4 block text-sm">文件名（即商品展示名）
      <input aria-label="文件名" className="field mt-2 !rounded-xl !px-3 !py-2 text-sm" value={name} onChange={event => setName(event.target.value)} placeholder="例如：定制酒杯.jpg" /></label>
    <p className="mt-3 text-xs text-slate-500">只支持 jpg、jpeg、png。追加后会单独打标；追加单品时，所有套装需要重新确认「包含的单品」。</p>
    <Failure error={error} />
    <div className="mt-5"><GlassButton disabled={busy || !valid} onClick={() => file && onAppend(kind, file, name.trim())}>{busy ? '正在上传…' : '上传并打标'}</GlassButton></div>
  </GlassModal>
}

export function RemoveModal({ name, isItem, onConfirm, onClose, busy, error }: Common & { name: string; isItem: boolean; onConfirm: () => void }) {
  return <GlassModal open title="移除主图" onClose={onClose}>
    <p className="text-sm">确定移除「{name}」吗？文件会移到本批次的回收区，不会入库。{isItem && '它会同时从所有套装的「包含的单品」里去掉。'}</p>
    <Failure error={error} />
    <div className="mt-5"><GlassButton disabled={busy} onClick={onConfirm}>确认移除</GlassButton></div>
  </GlassModal>
}

export function OwnerModal({ ownerId, onSave, onClose, busy, error }: Common & { ownerId: number; onSave: (userId: number) => void }) {
  const users = useQuery({ queryKey: ['roles'], queryFn: listUsers, retry: false })
  const [selected, setSelected] = useState(ownerId)
  const options = (users.data ?? []).filter(user => user.enabled && user.roles.some(role => role !== 'admin'))
  return <GlassModal open title="更换责任人" onClose={onClose}>
    <p className="text-sm text-slate-500">新责任人可以核对并提交本批次；只列出已登录过、启用且有上传权限的账号。</p>
    {users.isPending ? <p className="mt-4 text-sm">正在加载用户…</p> : users.isError ? <p role="alert" className="mt-4 text-sm text-red-800">{users.error.message}</p> :
      <select aria-label="新责任人" className="field mt-4 text-sm" value={selected} onChange={event => setSelected(Number(event.target.value))}>
        {options.map(user => <option key={user.id} value={user.id}>{user.display_name || user.username}</option>)}</select>}
    <Failure error={error} />
    <div className="mt-5"><GlassButton disabled={busy || selected === ownerId || !options.some(user => user.id === selected)} onClick={() => onSave(selected)}>保存</GlassButton></div>
  </GlassModal>
}
