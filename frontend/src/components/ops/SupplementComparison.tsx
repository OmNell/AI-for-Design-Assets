import { useState } from 'react'
import { supplementPreviewUrl, type SupplementComparison } from '../../api/audit'
import { formatBytes } from '../../lib/terms'
import { AssetPreview } from '../AssetPreview'
import { Button, Panel } from '../ui'

export function SupplementPictures({ batchId, version, original, uploaded }: {
  batchId: number; version?: string | null
  original: { id: number; rel_path: string }; uploaded: { id: number; rel_path: string }
}) {
  const [open, setOpen] = useState(false)
  return <div className="mt-2">
    <Button primary={false} aria-expanded={open} onClick={() => setOpen(value => !value)}>
      {open ? '收起新旧资料图片' : '并排预览新旧资料图片'}
    </Button>
    {open && <div className="mt-3 grid grid-cols-2 gap-3">
      <section className="min-w-0" aria-label="当前资料图片">
        <h4 className="mb-2 text-sm font-medium">当前资料图片</h4>
        <AssetPreview fileId={original.id} name={original.rel_path} verifiedOnly compact />
      </section>
      <section className="min-w-0" aria-label="新上传资料图片">
        <h4 className="mb-2 text-sm font-medium">新上传资料图片</h4>
        <AssetPreview fileId={uploaded.id} name={uploaded.rel_path} verifiedOnly compact
          stagedUrl={supplementPreviewUrl(batchId, uploaded.id, version)} />
      </section>
    </div>}
  </div>
}

export function SupplementAuditComparison({ comparison, batchId, version }: {
  comparison: SupplementComparison; batchId: number; version: string | null
}) {
  return <Panel><div className="space-y-3">
    <h2 className="text-lg font-semibold">补充文件核对</h2>
    <p className="text-sm text-slate-600">新增 {comparison.added} · 替换 {comparison.replaced} · 跳过 {comparison.skipped}
      {comparison.kept_both > 0 && `（新增含另存 ${comparison.kept_both}）`}</p>
    {comparison.materials.map(({ original, uploaded }) => <section key={uploaded.id} className="rounded-lg border border-slate-200 p-3">
      <h3 className="break-all text-sm font-medium">{uploaded.rel_path}</h3>
      <dl className="mt-2 grid grid-cols-2 gap-3 text-xs text-slate-600">
        {([['当前文件', original], ['新上传文件', uploaded]] as const).map(([label, file]) => <div key={label} className="min-w-0">
          <dt className="font-medium">{label}</dt>
          <dd>{formatBytes(file.size)} · {new Date(file.mtime).toLocaleString('zh-CN')}</dd>
          <dd className="break-all">SHA-256 {file.fingerprint}</dd>
        </div>)}
      </dl>
      {version && /\.(jpe?g|png)$/i.test(uploaded.rel_path) &&
        <SupplementPictures key={version} batchId={batchId} version={version} original={original} uploaded={uploaded} />}
    </section>)}
  </div></Panel>
}
