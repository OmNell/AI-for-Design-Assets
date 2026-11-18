import { request } from './client'

export interface IngestProgress {
  status: '入库中' | '入库异常' | '已入库'
  step: number
  step_name: string
  error: string | null
  products: number
  files: number
  vectors_ready: number
  can_resume: boolean
}

export const getIngest = (id: number): Promise<IngestProgress | null> => request(`/api/review/${id}/ingest`)
export const resumeIngest = (id: number): Promise<IngestProgress> => request(`/api/review/${id}/resume-ingest`, { method: 'POST' })
