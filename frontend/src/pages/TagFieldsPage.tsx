import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getTagFields, saveTagFields, type TagField, type TagFields } from '../api/ai'
import { AdminLinks } from '../components/AdminLinks'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'

const editable = (value: TagFields): TagField[] => [...value.fields].sort((a, b) => a.sort_order - b.sort_order)

function FieldsForm({ initial }: { initial: TagFields }) {
  const [fields, setFields] = useState<TagField[]>(() => editable(initial))
  useEffect(() => { setFields(editable(initial)) }, [initial])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const query = useQueryClient()
  const change = (index: number, hint: string) => setFields(values => values.map((field, position) => position === index ? { ...field, hint } : field))
  const move = (index: number, direction: -1 | 1) => setFields(values => {
    const ordered = [...values]
    const next = index + direction
    if (next >= 0 && next < values.length) [ordered[index], ordered[next]] = [ordered[next], ordered[index]]
    return ordered
  })
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setError(''); setMessage('')
    try {
      const orders = initial.fields.map(item => item.sort_order).sort((a, b) => a - b)
      const saved = await saveTagFields({ version: initial.version, fields: fields.map((field, index) => ({ key: field.key, hint: field.hint, sort_order: orders[index] })) })
      query.setQueryData(['tag-fields'], saved)
      setMessage('标签字段已保存')
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '保存失败，请重试') }
    finally { setSaving(false) }
  }
  return <form onSubmit={event => void save(event)} className="space-y-5">
    <p className="text-sm text-slate-500">当前版本 {initial.version} · 11 个固定字段。可调整填写说明和显示顺序。</p>
    {fields.map((field, index) => <div key={field.key} className="rounded-2xl border border-white/70 bg-white/20 p-5">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold">{field.label}</h2>
        <div className="flex items-center gap-3">
          <GlassButton primary={false} aria-label={`上移${field.label}`} disabled={index === 0} onClick={() => move(index, -1)}>上移</GlassButton>
          <GlassButton primary={false} aria-label={`下移${field.label}`} disabled={index === fields.length - 1} onClick={() => move(index, 1)}>下移</GlassButton>
        </div>
      </div>
      <label className="text-sm">填写说明
        <textarea className="field mt-2 min-h-24" value={field.hint} onChange={event => change(index, event.target.value)} />
      </label>
    </div>)}
    <GlassButton type="submit" disabled={saving}>{saving ? '正在保存…' : '保存字段'}</GlassButton>
    {message && <p role="status" className="text-sm text-emerald-800">{message}</p>}{error && <p role="alert" className="text-sm text-red-800">{error}</p>}
  </form>
}

export function TagFieldsPage() {
  const fields = useQuery({ queryKey: ['tag-fields'], queryFn: getTagFields, retry: false })
  return <GlassPanel><AdminLinks /><h1 className="mb-6 text-2xl font-semibold">标签字段管理</h1>
    {fields.isPending ? <p>正在加载字段…</p> : fields.isError ? <p role="alert" className="text-red-800">{fields.error.message}</p> : <FieldsForm initial={fields.data} />}
  </GlassPanel>
}
