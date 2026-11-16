import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getTagFields, saveTagFields, type TagField, type TagFields } from '../api/ai'
import { AdminLinks } from '../components/AdminLinks'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'

interface EditableField extends TagField { persisted: boolean; rowId: string }
const editable = (value: TagFields): EditableField[] => [...value.fields].sort((a, b) => a.sort_order - b.sort_order).map(field => ({ ...field, persisted: true, rowId: field.key }))

function FieldsForm({ initial }: { initial: TagFields }) {
  const [fields, setFields] = useState<EditableField[]>(() => editable(initial))
  useEffect(() => { setFields(editable(initial)) }, [initial])
  const nextId = useRef(0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const query = useQueryClient()
  const change = (index: number, patch: Partial<TagField>) => setFields(values => values.map((field, position) => position === index ? { ...field, ...patch } : field))
  const move = (index: number, direction: -1 | 1) => setFields(values => {
    const ordered = [...values]
    const next = index + direction
    if (next >= 0 && next < values.length) [ordered[index], ordered[next]] = [ordered[next], ordered[index]]
    return ordered
  })
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setError(''); setMessage('')
    try {
      const saved = await saveTagFields({ version: initial.version, fields: fields.map((field, index) => ({ key: field.key, label: field.label, hint: field.hint, enabled: field.enabled, sort_order: index })) })
      query.setQueryData(['tag-fields'], saved)
      setMessage('标签字段已保存')
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '保存失败，请重试') }
    finally { setSaving(false) }
  }
  return <form onSubmit={event => void save(event)} className="space-y-5">
    <p className="text-sm text-slate-500">当前版本 {initial.version} · 所有字段均为自由文本，不限数量，没有人工必填。删除会停用字段，历史值保留。</p>
    {fields.map((field, index) => <div key={field.rowId} className={`rounded-2xl border border-white/70 bg-white/20 p-5 ${field.enabled ? '' : 'opacity-60'}`}>
      <div className="mb-4 flex items-center justify-between"><label className="text-sm"><input type="checkbox" checked={field.enabled} onChange={event => change(index, { enabled: event.target.checked })} className="mr-2" />启用 {field.label || '新字段'}</label>
        <div className="flex items-center gap-3"><GlassButton primary={false} aria-label={`上移${field.label || '新字段'}`} disabled={index === 0} onClick={() => move(index, -1)}>上移</GlassButton><GlassButton primary={false} aria-label={`下移${field.label || '新字段'}`} disabled={index === fields.length - 1} onClick={() => move(index, 1)}>下移</GlassButton><GlassButton primary={false} disabled={!field.enabled && field.persisted} onClick={() => field.persisted ? change(index, { enabled: false }) : setFields(values => values.filter((_, position) => position !== index))}>删除</GlassButton></div></div>
      <div className="grid grid-cols-3 gap-4"><label className="text-sm">字段标识<input className="field mt-2" required pattern="[a-z][a-z0-9_]*" readOnly={field.persisted} value={field.key} onChange={event => change(index, { key: event.target.value })} /></label><label className="text-sm">显示名称<input className="field mt-2" required value={field.label} onChange={event => change(index, { label: event.target.value })} /></label><label className="text-sm">填写说明<textarea className="field mt-2 min-h-24" value={field.hint} onChange={event => change(index, { hint: event.target.value })} /></label></div>
      {!field.enabled && <p className="mt-3 text-xs text-slate-500">已停用，历史值保留</p>}
    </div>)}
    <div className="flex gap-4"><GlassButton primary={false} onClick={() => setFields(values => [...values, { key: '', label: '', hint: '', sort_order: values.length, enabled: true, persisted: false, rowId: `new-${nextId.current++}` }])}>新增字段</GlassButton><GlassButton type="submit" disabled={saving}>{saving ? '正在保存…' : '保存字段'}</GlassButton></div>
    {message && <p role="status" className="text-sm text-emerald-800">{message}</p>}{error && <p role="alert" className="text-sm text-red-800">{error}</p>}
  </form>
}

export function TagFieldsPage() {
  const fields = useQuery({ queryKey: ['tag-fields'], queryFn: getTagFields, retry: false })
  return <GlassPanel><AdminLinks /><h1 className="mb-6 text-2xl font-semibold">标签字段管理</h1>
    {fields.isPending ? <p>正在加载字段…</p> : fields.isError ? <p role="alert" className="text-red-800">{fields.error.message}</p> : <FieldsForm initial={fields.data} />}
  </GlassPanel>
}
