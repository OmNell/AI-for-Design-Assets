import { Button, Panel } from '../components/ui'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { getAiAlerts, getAiSettings, saveAiSettings, testAiModels, type AiSettings, type AiSettingsInput, type ModelTest } from '../api/ai'
import { VectorIndexPanel } from '../components/VectorIndexPanel'

const editable = (value: AiSettings): AiSettingsInput => ({ base_url: value.base_url, model_vision: value.model_vision, model_text: value.model_text, model_embed: value.model_embed, concurrency: value.concurrency, timeout_s: value.timeout_s, retries: value.retries, stall_alert_min: value.stall_alert_min, api_key: '' })

function SettingsForm({ initial }: { initial: AiSettings }) {
  const query = useQueryClient()
  const { register, handleSubmit, reset, resetField, formState: { isSubmitting } } = useForm<AiSettingsInput>({ defaultValues: editable(initial) })
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [testing, setTesting] = useState(false)
  const [results, setResults] = useState<ModelTest[]>([])
  const save = handleSubmit(async value => {
    setError(''); setMessage(''); setResults([])
    resetField('api_key')
    try {
      const saved = await saveAiSettings(value)
      query.setQueryData(['ai-settings'], saved)
      reset(editable(saved))
      setMessage('AI 设置已保存')
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '保存失败，请重试') }
  })
  const test = async () => {
    setTesting(true); setResults([]); setError('')
    try { setResults((await testAiModels()).results) }
    catch (reason: unknown) { setError(reason instanceof Error ? reason.message : '测试未完成，请重试') }
    finally { setTesting(false) }
  }
  return <form onSubmit={save} className="mt-7 space-y-6">
    <label className="block text-sm">接口地址<input className="field mt-2" required type="url" {...register('base_url')} /></label>
    <label className="block text-sm">API Key <span className="ml-2 text-xs text-slate-500">{initial.api_key_set ? '已设置' : '尚未设置'}</span><input className="field mt-2" type="password" autoComplete="new-password" placeholder={initial.api_key_set ? '留空保留已保存的密钥' : '输入 API Key'} {...register('api_key')} /></label>
    <div className="field-grid">
      <label className="text-sm">识图模型<input className="field mt-2" {...register('model_vision')} /></label>
      <label className="text-sm">文本模型<input className="field mt-2" {...register('model_text')} /></label>
      <label className="text-sm">初始向量模型<input className="field mt-2" {...register('model_embed')} /></label>
    </div>
    <div className="field-grid">
      <label className="text-sm">并发数<input className="field mt-2" type="number" min={1} required {...register('concurrency', { valueAsNumber: true })} /></label>
      <label className="text-sm">超时（秒）<input className="field mt-2" type="number" min={1} required {...register('timeout_s', { valueAsNumber: true })} /></label>
      <label className="text-sm">重试次数<input className="field mt-2" type="number" min={0} required {...register('retries', { valueAsNumber: true })} /></label>
      <label className="text-sm">停滞提醒（分钟）<input className="field mt-2" type="number" min={1} required {...register('stall_alert_min', { valueAsNumber: true })} /></label>
    </div>
    <div className="flex items-center gap-4"><Button type="submit" disabled={isSubmitting || testing}>保存设置</Button><Button primary={false} onClick={() => void test()} disabled={isSubmitting || testing}>{testing ? '正在测试…' : '测试连接'}</Button><span className="text-xs text-slate-500">测试连接使用已保存的配置</span></div>
    {message && <p role="status" className="text-sm text-emerald-800">{message}</p>}
    {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    {!!results.length && <ul className="space-y-3 text-sm">{results.map(result => <li key={result.kind} className="rounded-xl bg-slate-50 p-4"><p className={result.success ? 'text-emerald-800' : 'text-red-800'}>{{ vision: '识图模型', text: '文本模型', embed: '向量模型' }[result.kind] ?? result.kind} · {result.model || '未配置模型'} · {result.success ? '连接成功' : '测试未通过'}</p>{result.error && <p className="mt-2 whitespace-pre-wrap break-words text-xs text-red-800">{result.error}</p>}</li>)}</ul>}
  </form>
}

export function AiSettingsPage() {
  const settings = useQuery({ queryKey: ['ai-settings'], queryFn: getAiSettings, retry: false })
  const alerts = useQuery({ queryKey: ['ai-alerts'], queryFn: getAiAlerts, retry: false, refetchInterval: 30000 })
  return <div className="space-y-6"><Panel><h1 className="text-2xl font-semibold">AI 设置</h1><Link className="text-sm text-blue-700 underline" to="/admin/maintenance?tab=settings">系统参数</Link><p className="mt-3 text-sm text-slate-500">配置百炼兼容接口与三个模型。密钥保存后只显示是否已设置。</p>
    {settings.isPending ? <p className="mt-6">正在加载设置…</p> : settings.isError ? <p role="alert" className="mt-6 text-red-800">{settings.error.message}</p> : <SettingsForm initial={settings.data} />}
  </Panel><VectorIndexPanel /><Panel><h2 className="text-xl font-semibold">AI 停滞异常</h2><div className="mt-5 space-y-3">{alerts.isPending ? <p className="text-sm text-slate-500">正在加载异常记录…</p> : alerts.isError ? <p role="alert" className="text-sm text-red-800">{alerts.error.message}</p> : alerts.data.length ? alerts.data.map(alert => <div key={alert.id} className="rounded-xl bg-amber-100/40 p-4 text-sm"><Link to={`/batches/${alert.batch_id}`} className="font-medium text-blue-700 underline underline-offset-4">查看批次</Link><p className="mt-2">{alert.reason}</p><p className="mt-2 text-xs text-slate-500">{new Date(alert.detected_at).toLocaleString('zh-CN')}</p></div>) : <p className="text-sm text-slate-500">暂无停滞异常</p>}</div></Panel></div>
}
