import { request } from './client'

const path = '/api/admin/recovery'

type RecoveryAction = 'detect' | 'scan' | 'apply' | 'stage'
export type RecoveryRequest =
  | { action: 'detect' | 'scan' }
  | { action: 'apply'; plan_id: number; confirmed: true }
  | { action: 'stage'; backup_name: string; confirmed: true }

export interface RecoveryRun {
  id: number
  action: RecoveryAction
  status: string
  error: string | null
  actor: string | null
  started_at: string | null
  finished_at: string | null
  progress: { phase?: string; code?: string; project_id?: number; products?: number } | null
  summary: { recoverable?: number; conflicts?: number; unresolved?: number; recovered?: number; failed?: number; backup_candidates?: number } | null
}

export interface RecoveryStatus {
  catalog_state: string
  nas_state: string
  nas_checked_at: string | null
  projects: number | null
  products: number | null
  files: number | null
  last_backup_at: string | null
  can_backup: boolean
  read_only?: boolean
  runs: RecoveryRun[]
}

export interface ArchiveCandidate {
  path: string
  version: number | null
  error: string | null
  publication_receipt: boolean
}

export interface RecoveryItem {
  kind: 'missing' | 'incomplete' | 'newer' | 'same' | 'conflict' | 'local_only'
  project_id: number
  code: string
  path: string
  version: number | null
  reason: string
  products?: number
}
export interface RecoveryDifference {
  items?: RecoveryItem[]
  unresolved?: { project_code: string; reason: string; candidates: ArchiveCandidate[] }[]
  inaccessible?: { path: string; reason: string }[]
  not_recoverable?: string[]
}
export interface RecoveryBackup {
  name: string
  size: number
  app_version?: string | null
  format: number | null
  validation: string
  modified_at: string
}
export interface RecoveryReport extends RecoveryDifference {
  nas_state?: string
  backup_state?: string
  backups?: RecoveryBackup[]
  recovered?: number[]
  skipped?: number[]
  blocked?: { project_id: number; reason: string }[]
  stage_dir?: string
  inventory?: Record<string, { count: number }>
  source_revision?: string
  revision?: string
  difference?: RecoveryDifference | null
}

export const getRecovery = (): Promise<RecoveryStatus> => request(path)
export const getRecoveryReport = (id: number): Promise<RecoveryReport> => request(`${path}/reports/${id}`)
export const runRecovery = (data: RecoveryRequest): Promise<RecoveryRun> => {
  const { action, ...body } = data
  return request(`${path}/${action}`, { method: 'POST', body: JSON.stringify(body) })
}
