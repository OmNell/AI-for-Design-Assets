import type { SearchMode } from './search'
import { request } from './client'

const api = '/api/admin'
const post = <T>(path: string, data: unknown = {}): Promise<T> => request(path, { method: 'POST', body: JSON.stringify(data) })
export type SystemJob = 'inspection' | 'backup'
export interface SystemParameters {
  session_hours: number
  chunk_mb: number
  parallel_files: number
  overdue_days: number
  cooling_days: number
  inspection_time: string
  backup_time: string
  timezone: 'Asia/Shanghai'
}
export interface Run {
  id: number; status: string; started_at: string; finished_at: string | null; error: string | null
  summary: { projects?: number; files?: number; count?: number; auto_fixed?: number; manual?: number; elapsed_ms?: number; kinds?: Record<string, number> }
}
export interface Finding {
  id: number; run_id: number | null; kind: string; path: string; project_id: number | null; file_id: number | null
  detail: string; auto_fixed: boolean; created_at: string; acknowledged_at: string | null; acknowledged_by: number | null
  resolved_at: string | null
}
export interface Backup {
  id: number; job_id: number; name: string; status: string; size: number | null; verified_at: string | null
  created_at: string; local_retained: boolean; error: string | null
}
export interface CleanupTarget {
  key: string; label: string; kind: string; batch_id: number | null; project_code: string | null; owner: string | null
  eligible: boolean; status: string; created_at: string | null; updated_at: string | null; size: number; files: number
}
export interface CleanupPreview {
  id: number; target: string; entries: { path: string; size: number; fingerprint: string }[]
}
export interface ErrorItem {
  kind: string; reason: string; object_id: number; batch_id: number | null; job_id: number | null
  retry: 'system' | 'candidate' | 'actor' | null; generation: number | null
}
export interface LogBase { id: number; ts: string; user_id: number | null; username: string | null }
export interface OperationLog extends LogBase {
  role: string; action: string; result: string; object_type: string; object_id: string | null; client_host: string
  before: unknown; after: unknown
}
export interface SearchLog extends LogBase {
  query: string; mode: SearchMode; result_count: number; clicks: number; duration_ms: number; warnings: string[]
}
export interface LogPage<T> { total: number; items: T[] }
export interface RepairCandidate { project_id: number; project_code: string; project_name: string; ready: boolean; detail: string }
export interface RepairResult { project_id: number; batch_id: number | null; error: string | null }
export interface ThumbnailStatus { total: number; ready: number; missing: number; running: number }

export const getSystem = (): Promise<SystemParameters> => request('/api/settings/system')
export const saveSystem = (value: SystemParameters): Promise<SystemParameters> => request('/api/settings/system', { method: 'PUT', body: JSON.stringify(value) })
export const getRuns = (query: string): Promise<Run[]> => request(`${api}/inspection/runs?${query}`)
export const getFindings = (query: string): Promise<Finding[]> => request(`${api}/inspection/findings?${query}`)
export const ackFinding = (id: number): Promise<Finding> => post(`${api}/inspection/findings/${id}/ack`)
export const runMaintenance = (kind: SystemJob): Promise<unknown> => post(`${api}/maintenance/${kind}/run`)
export const getBackups = (): Promise<Backup[]> => request(`${api}/backups`)
export const backupDownload = (id: number) => `${api}/backups/${id}/download`
export const getCleanup = (): Promise<CleanupTarget[]> => request(`${api}/temp-cleanup`)
export const previewCleanup = (key: string): Promise<CleanupPreview> => post(`${api}/temp-cleanup/preview/${key}`)
export const executeCleanup = (id: number, confirmation: string): Promise<CleanupPreview> => post(`${api}/temp-cleanup/execute/${id}`, { confirmation })
export const getOpsLogs = (query: string): Promise<LogPage<OperationLog>> => request(`${api}/oplog?${query}`)
export const getSearchLogs = (query: string): Promise<LogPage<SearchLog>> => request(`${api}/search-logs?${query}`)
export const exportLogs = (search: boolean, query: string) => `${api}/${search ? 'search-logs' : 'oplog'}/export?${query}`
export const getErrors = (): Promise<ErrorItem[]> => request(`${api}/maintenance/errors`)
export const retrySystem = (job: number): Promise<unknown> => post(`${api}/jobs/${job}/retry`)
export const getThumbnailStatus = (): Promise<ThumbnailStatus> => request(`${api}/thumbnails/status`)
export const rebuildThumbs = (product_ids: number[], missing_only = false, all_missing = false): Promise<{ id: number }[]> =>
  post(`${api}/thumbnails/rebuild`, { product_ids, ...(missing_only && { missing_only }), ...(all_missing && { all_missing }) })
export const getRepairs = (): Promise<RepairCandidate[]> => request('/api/archive/repairs')
export const createRepairs = (project_ids: number[]): Promise<RepairResult[]> => post('/api/archive/repairs', { project_ids })
