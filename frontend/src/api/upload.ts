import { request } from './client'
import type { Issue, ManifestEntry } from '../lib/preflight'
export interface UploadFile { id: number; rel_path: string; size: number; mtime: number; upload_status: string; bytes_received: number; fingerprint: string | null; thumb_status: 'pending' | 'running' | 'done' | 'failed' | null; thumb_error: string | null }
export interface Batch { id: number; batch_no: string; folder_name: string; original_folder_name: string; project_code: string; status: string; queued: boolean; won: boolean; updated_at: string; files: UploadFile[]; directories: string[]; chunk_bytes: number; parallel_files: number; upload_token: string | null; differences: Record<string, string[]> | null }
export interface Precheck { project_code: string; company_name: string; project_name: string; code_status: 'new' | 'exists' | 'need_h_code'; issues: Issue[]; path_prefix_length?: number }
export interface SelectionBody { folder_name: string; files: ManifestEntry[] }
const headers = (token?: string | null): Record<string, string> => token ? { 'X-Upload-Token': token } : {}
export const checkUpload = (body: SelectionBody): Promise<Precheck> => request('/api/batches/precheck', { method: 'POST', body: JSON.stringify(body) })
export const createBatch = (body: SelectionBody & { won: boolean; historical_year: number; acks: string[] }): Promise<Batch> => request('/api/batches', { method: 'POST', body: JSON.stringify({ ...body, kind: 'ingest' }) })
export const getBatches = (): Promise<Batch[]> => request('/api/batches')
export const getBatch = (id: number): Promise<Batch> => request(`/api/batches/${id}`)
export const getUploadState = (id: number, token?: string | null): Promise<Batch> => request(`/api/batches/${id}/upload-state`, { headers: headers(token) })
export const resumeBatch = (id: number, body: SelectionBody & { acks: string[]; accept_changes: boolean }): Promise<Batch> => request(`/api/batches/${id}/resume`, { method: 'POST', body: JSON.stringify(body) })
export const sendChunk = (id: number, file: number, index: number, data: Blob, token: string, signal: AbortSignal): Promise<{ bytes_received: number }> => request(`/api/batches/${id}/files/${file}/chunks/${index}`, { method: 'PUT', body: data, headers: { ...headers(token), 'Content-Type': 'application/octet-stream' }, signal })
export const completeFile = (id: number, file: number, token: string): Promise<Batch> => request(`/api/batches/${id}/files/${file}/complete`, { method: 'POST', headers: headers(token) })
export const pauseBatch = (id: number, token?: string | null, keepalive = false): Promise<Batch> => request(`/api/batches/${id}/pause`, { method: 'POST', headers: headers(token), keepalive })
export const cancelBatch = (id: number, token?: string | null): Promise<Batch> => request(`/api/batches/${id}/cancel`, { method: 'POST', headers: headers(token) })
export const confirmBatch = (id: number): Promise<Batch> => request(`/api/batches/${id}/confirm`, { method: 'POST' })
