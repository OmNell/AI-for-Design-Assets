import { Button } from '../ui'
import { useEffect, useId, useRef, useState } from 'react'
import { getTagValues, type ProductPatch, type ReviewField, type ReviewProduct, type TagValue } from '../../api/review'
import type { ProductTag } from '../../api/upload'
import { lastSegment, removeTerm, replaceLastSegment, splitTerms } from '../../lib/terms'

const sourceLabels: Record<string, string> = { ai: 'AI', manual: '已改', ai_confirmed: '已确认' }
const flagReasons: Record<string, string> = { uncertain: '模型无法确定，可填写具体值或确认留空。' }

function SourceBadge({ tag }: { tag: ProductTag | undefined }) {
  if (!tag) return <span className="shrink-0 rounded-full bg-slate-50 px-2 py-0.5 text-[11px] text-slate-500">未填</span>
  const manual = tag.source === 'manual'
  // 人工改过的字段悬停可见「AI 原值 → 人工值」。
  return <span data-source={tag.source} title={manual ? `AI 原值：${tag.ai_value || '（空）'} → 人工值：${tag.value || '（空）'}` : tag.source === 'ai_confirmed' ? '已确认 AI 值正确' : 'AI 填写'}
    className={`shrink-0 cursor-default rounded-full px-2 py-0.5 text-[11px] ${manual ? 'bg-emerald-800 text-white' : 'bg-slate-50 text-slate-600'}`}>{sourceLabels[tag.source] ?? tag.source}</span>
}

interface FieldProps { batchId: number; showAi?: boolean; field: ReviewField; tag: ProductTag | undefined; editable: boolean; canConfirm: boolean; onChange: (patch: ProductPatch, immediate?: boolean) => void }
export function FieldEditor({ batchId, field, tag, editable, canConfirm, onChange, showAi = true }: FieldProps) {
  const id = useId()
  const server = tag?.value ?? ''
  const [text, setText] = useState(server)
  const [focused, setFocused] = useState(false)
  const [options, setOptions] = useState<TagValue[]>([])
  const latest = useRef({ text, focused })
  latest.current = { text, focused }
  useEffect(() => {
    // 服务端只去掉首尾空白时，不打断正在输入的内容。
    const { text: local, focused: typing } = latest.current
    if (server !== local && !(typing && server === local.trim())) setText(server)
  }, [server])
  useEffect(() => {
    if (!focused || !editable) { setOptions([]); return }
    const controller = new AbortController()
    const timer = setTimeout(() => {
      getTagValues(field.key, lastSegment(text), batchId, controller.signal).then(values => {
        if (!controller.signal.aborted) setOptions(values)
      }).catch(() => undefined)
    }, 250)
    return () => { clearTimeout(timer); controller.abort() }
  }, [focused, editable, text, field.key, batchId])
  const update = (value: string) => { setText(value); onChange({ tags: { [field.key]: value } }) }
  const terms = splitTerms(text)
  const hints = options.filter(option => !terms.includes(option.term))
  return <div data-field={field.key} data-flag={tag?.flag ?? ''} className={`rounded-xl p-3 ${tag?.flag ? 'bg-amber-100/75 ring-1 ring-amber-500/50' : 'bg-slate-50'}`}>
    <div className="flex items-baseline gap-2">
      <label htmlFor={id} className="shrink-0 text-sm font-medium">{field.label}</label>
      <span className="min-w-0 flex-1 truncate text-xs text-slate-500" title={field.hint}>{field.hint}</span>
      <SourceBadge tag={tag} />
    </div>
    {!!terms.length && <ul className="mt-2 flex flex-wrap gap-1.5" aria-label={`${field.label}的值`}>{terms.map(term => <li key={term} className="flex items-center gap-1 rounded-full bg-white py-0.5 pl-2.5 pr-1 text-xs">
      <span className="max-w-48 truncate">{term}</span>
      {editable && <button type="button" aria-label={`删除 ${term}`} className="h-4 w-4 rounded-full leading-none text-slate-500 hover:bg-slate-200" onClick={() => update(removeTerm(text, term))}>×</button>}</li>)}</ul>}
    <div className="relative mt-2">
      <input id={id} className="field !rounded-xl !px-3 !py-2 text-sm disabled:opacity-70" value={text} disabled={!editable} maxLength={500} autoComplete="off"
        placeholder="可写多个值，用顿号「、」分隔" onChange={event => update(event.target.value)}
        onFocus={() => setFocused(true)} onBlur={() => setFocused(false)} onKeyDown={event => { if (event.key === 'Escape') setOptions([]) }} />
      {focused && !!hints.length && <ul role="listbox" aria-label={`${field.label}已用过的说法`} className="absolute left-0 right-0 top-full z-20 mt-1 max-h-52 overflow-y-auto rounded-xl border border-slate-200 bg-white py-1 text-sm shadow-lg">
        <li className="px-3 py-1 text-[11px] text-slate-400">库里用过的说法，仅供参考</li>
        {hints.map(option => <li key={option.term} role="option" aria-selected={false} className="flex cursor-pointer justify-between gap-3 px-3 py-1.5 hover:bg-emerald-50"
          // 用 mousedown 而不是 click，输入框不会先失焦把下拉收起。
          onMouseDown={event => { event.preventDefault(); update(replaceLastSegment(text, option.term)) }}>
          <span className="truncate">{option.term}</span><span className="shrink-0 text-xs text-slate-400">{option.count} 次</span></li>)}</ul>}
    </div>
    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
      <span>{tag?.confirmation_status === 'confirmed_unknown' ? '已确认无法判断' : tag?.confirmation_status === 'confirmed_value' ? '已确认具体值' : '尚未确认'}</span>
      {editable && <>
        <button type="button" className="text-blue-700 underline disabled:opacity-40" disabled={!text.trim() || !canConfirm || tag?.confirmation_status === 'confirmed_value'}
          onClick={() => onChange({ tags: { [field.key]: text }, confirmations: { [field.key]: 'confirmed_value' } }, true)}>确认具体值</button>
        <button type="button" className="text-blue-700 underline disabled:opacity-40" disabled={!canConfirm || tag?.confirmation_status === 'confirmed_unknown'}
          onClick={() => onChange({ confirmations: { [field.key]: 'confirmed_unknown' } }, true)}>确认无法判断（留空）</button>
      </>}
    </div>
    {tag?.flag && <p className="mt-2 text-xs text-amber-900">{flagReasons[tag.flag] ?? '该字段需要核对。'}</p>}
    {showAi && <div className={tag?.previous_value == null ? 'mt-2 text-xs text-slate-600' : 'mt-2 grid grid-cols-2 gap-2 text-xs text-slate-600'}>
      {tag?.previous_value != null && <div><p>替换前</p><p className="mt-1 break-words">{tag.previous_value || '（空）'}</p>
        {editable && <button type="button" className="mt-1 underline disabled:opacity-40" disabled={!canConfirm} onClick={() => {
          const value = tag.previous_value ?? ''
          setText(value)
          onChange({ tags: { [field.key]: value }, confirmations: { [field.key]: value.trim() ? 'confirmed_value' : 'confirmed_unknown' } }, true)
        }}>沿用旧值</button>}</div>}
      <div><p>AI 原值</p><p className="mt-1 break-words">{tag?.ai_value || '（空）'}</p>
        {tag?.previous_value != null && editable && <button type="button" className="mt-1 underline disabled:opacity-40" disabled={!canConfirm || !tag.ai_value.trim() || tag.ai_value === '无法判断'} onClick={() => {
          setText(tag.ai_value)
          onChange({ tags: { [field.key]: tag.ai_value }, confirmations: { [field.key]: 'confirmed_value' } }, true)
        }}>采用 AI 新值</button>}</div>
    </div>}
  </div>
}

function FreeText({ product, editable, onChange }: { product: ReviewProduct; editable: boolean; onChange: (patch: ProductPatch) => void }) {
  const id = useId()
  const [text, setText] = useState(product.free_text)
  const latest = useRef({ text, focused: false })
  latest.current.text = text
  useEffect(() => {
    const { text: local, focused } = latest.current
    if (product.free_text !== local && !(focused && product.free_text === local.trim())) setText(product.free_text)
  }, [product.free_text])
  const manual = product.free_text_source === 'manual'
  return <div className="rounded-xl bg-slate-50 p-3">
    <div className="flex items-baseline justify-between gap-2"><label htmlFor={id} className="text-sm font-medium">自由描述</label>
      <span className="flex-1 text-xs text-slate-500">AI 生成，可改；用于搜索兜底</span>
      <span title={manual ? `AI 原值：${product.ai_free_text || '（空）'}` : 'AI 填写'} className={`rounded-full px-2 py-0.5 text-[11px] ${manual ? 'bg-emerald-800 text-white' : 'bg-slate-50 text-slate-600'}`}>{manual ? '已改' : 'AI'}</span></div>
    <textarea id={id} rows={4} className="field mt-2 !rounded-xl !px-3 !py-2 text-sm disabled:opacity-70" value={text} disabled={!editable} maxLength={5000}
      onFocus={() => { latest.current.focused = true }} onBlur={() => { latest.current.focused = false }}
      onChange={event => { setText(event.target.value); onChange({ free_text: event.target.value }) }} />
  </div>
}

export interface TagFormProps {
  batchId: number; product: ReviewProduct; fields: ReviewField[]; items: ReviewProduct[]; editable: boolean; busy: boolean
  onChange: (patch: ProductPatch, immediate?: boolean) => void
  onRetry: () => void; onRemove: () => void
  relationDraft: number[]; onRelationDraft: (ids: number[]) => void; onConfirmRelations: () => void
}

export function TagForm({ batchId, product, fields, items, editable, busy, onChange, onRetry, onRemove, relationDraft, onRelationDraft, onConfirmRelations }: TagFormProps) {
  const recommended = new Map(product.relation_items.map(item => [item.batch_product_id, item.source]))
  const saved = product.relation_items.map(item => item.batch_product_id)
  const changed = saved.length !== relationDraft.length || saved.some(value => !relationDraft.includes(value))
  return <div className="space-y-3">
    <div className="flex items-start justify-between gap-3"><div className="min-w-0"><h2 className="truncate text-lg font-semibold" title={product.file_name}>{product.display_name}</h2>
      <p className="mt-1 text-xs text-slate-500">{product.kind === 'set' ? '套装' : '单品'} · {product.file_name}</p></div>
      {editable && <button type="button" disabled={busy} className="shrink-0 text-xs text-red-800 underline underline-offset-4 disabled:opacity-50" onClick={onRemove}>移除这张图</button>}</div>
    {product.ai_status === 'pending' && <p role="status" className="rounded-xl bg-sky-100/70 p-3 text-sm text-sky-900">正在打标，完成后自动刷新。期间可以先手工填写。</p>}
    {(product.ai_status === 'failed' || (product.ai_status === 'done' && !product.visual_ai_succeeded)) && <div className="rounded-xl bg-red-100/75 p-3 text-sm text-red-900 ring-1 ring-red-400/40">
      <p>打标失败：{product.ai_error || '未知原因'}</p>
      <p className="mt-1 text-xs">当前主图缺少可提交的视觉识别结果，请重新打标。</p>
      {editable && <div className="mt-2"><Button disabled={busy} onClick={onRetry}>重新打标</Button></div>}</div>}
    {product.flags.includes('multi_object') && <div className="flex items-center justify-between gap-3 rounded-xl bg-amber-100/75 p-3 text-xs text-amber-900 ring-1 ring-amber-500/50">
      <p>这张单品图里识别出多个商品，请核对标签是否对应同一件商品。</p>
      {editable && <button type="button" className="shrink-0 rounded-full bg-amber-700 px-3 py-1 text-white hover:bg-amber-800" onClick={() => onChange({ resolve_flags: ['multi_object'] }, true)}>已核对，无需修改</button>}</div>}
    {editable && product.ai_status === 'done' && product.visual_ai_succeeded && <button type="button" disabled={busy} className="text-xs text-blue-700 underline underline-offset-4 disabled:opacity-50" onClick={onRetry}>重新打标（不覆盖人工改过的字段）</button>}
    {fields.map(field => <FieldEditor key={`${product.id}:${field.key}`} batchId={batchId} field={field} tag={product.tags[field.key]} canConfirm={product.visual_ai_succeeded && product.ai_status === 'done'} editable={editable} onChange={onChange} />)}
    <FreeText key={product.id} product={product} editable={editable} onChange={onChange} />
    {product.kind === 'set' && <fieldset data-testid="relations" className={`rounded-xl p-3 ${product.relation_confirmed && !changed ? 'bg-slate-50' : 'bg-amber-100/75 ring-1 ring-amber-500/50'}`}>
      <legend className="float-left mb-2 text-sm font-medium">包含的单品</legend>
      <p className="clear-both text-xs text-slate-600">{product.flags.includes('relation_recheck') ? '批次里追加了新的单品，请复核后重新确认。' :
        product.flags.includes('empty_relation') ? 'AI 没有推荐任何单品，请勾选后确认；确实不包含也要点确认。' :
        product.relation_confirmed ? changed ? '勾选有改动，尚未确认。' : product.relation_confirm_kind === 'confirmed_empty' ? '已确认无关联单品。' : '关系已确认。' : 'AI 推荐的已勾选，请核对后确认。'}</p>
      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5">{items.map(item => <label key={item.id} className="flex min-w-0 items-center gap-2 text-sm">
        <input type="checkbox" disabled={!editable} checked={relationDraft.includes(item.id)}
          onChange={event => onRelationDraft(event.target.checked ? [...relationDraft, item.id] : relationDraft.filter(value => value !== item.id))} />
        <span className="truncate" title={item.display_name}>{item.display_name}</span>
        {recommended.get(item.id) === 'ai' && <span className="shrink-0 text-[10px] text-slate-400">AI</span>}</label>)}
        {!items.length && <p className="col-span-2 text-xs text-slate-500">本批次没有单品主图。</p>}</div>
      {editable && <div className="mt-3"><Button disabled={busy || !product.visual_ai_succeeded || product.ai_status !== 'done' || (product.relation_confirmed && !changed)} onClick={onConfirmRelations}>{relationDraft.length ? '确认关系' : '确认无关联单品'}</Button></div>}
    </fieldset>}
  </div>
}
