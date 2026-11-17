import type { ChangePreview } from '../../api/ops'

/** 入库后操作的「变更预览」：改前与改后、涉及的商品和文件。审核页和操作详情页共用。 */
export function ChangePreviewPanel({ preview }: { preview: ChangePreview }) {
  return <section data-testid="change-preview" aria-label="变更预览" className="space-y-3 text-sm">
    <h3 className="font-semibold">变更预览 · {preview.kind_label}</h3>
    {preview.reason && <p><span className="text-slate-500">原因：</span>{preview.reason}</p>}
    {!!preview.rows.length && <table className="w-full text-left"><thead className="text-xs text-slate-500"><tr><th className="p-2">项目</th><th className="p-2">改前</th><th className="p-2">改后</th></tr></thead>
      <tbody>{preview.rows.map((row, index) => <tr key={index} className="border-t border-slate-500/10 align-top"><td className="p-2">{row.label}</td>
        <td className="break-all p-2 text-slate-600">{row.before || '（空）'}</td><td className="break-all p-2 font-medium">{row.after || '（空）'}</td></tr>)}</tbody></table>}
    {!!preview.products.length && <div><p className="text-xs text-slate-500">涉及商品（{preview.products.length}）</p><ul className="mt-1 space-y-0.5">{preview.products.map(item => <li key={item}>{item}</li>)}</ul></div>}
    {!!preview.files.length && <div><p className="text-xs text-slate-500">涉及文件（{preview.files.length}）</p><ul className="mt-1 max-h-48 space-y-0.5 overflow-auto">{preview.files.map(item => <li key={item} className="break-all">{item}</li>)}</ul></div>}
    {preview.notes.map(note => <p key={note} className="rounded-xl bg-amber-100/60 px-3 py-2 text-amber-900">{note}</p>)}
  </section>
}
