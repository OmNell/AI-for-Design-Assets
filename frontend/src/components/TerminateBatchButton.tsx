import { Button, Dialog } from './ui'
import { useState } from 'react'
import { terminateBatch, type Batch } from '../api/upload'

export function TerminateBatchButton({ id, disabled, onTerminated }: {
  id: number
  disabled?: boolean
  onTerminated: (batch: Batch) => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const terminate = async () => {
    setBusy(true)
    setError('')
    try {
      const batch = await terminateBatch(id, reason.trim())
      onTerminated(batch)
      setOpen(false)
    } catch (failure: unknown) {
      setError(failure instanceof Error ? failure.message : '终止失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }
  return <>
    <Button primary={false} disabled={disabled || busy} onClick={() => { setReason(''); setError(''); setOpen(true) }}>终止批次</Button>
    <Dialog open={open} title="终止此批次" onClose={() => { if (!busy) setOpen(false) }}>
      <form onSubmit={event => { event.preventDefault(); void terminate() }} className="space-y-4">
        <p className="text-sm">终止后无法继续此批次。上传的文件留在 NAS 临时区，由管理员清理。</p>
        <label className="block text-sm">终止原因
          <textarea className="field mt-2" value={reason} onChange={event => setReason(event.target.value)} maxLength={500} required />
        </label>
        {error && <p role="alert" className="text-red-800">{error}</p>}
        <Button type="submit" disabled={busy || !reason.trim()}>确认终止</Button>
      </form>
    </Dialog>
  </>
}
