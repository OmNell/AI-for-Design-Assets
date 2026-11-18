import { request } from './client'
import type { Issue, ManifestEntry } from '../lib/preflight'
export interface UploadFile { id: number; rel_path: string; size: number; mtime: number; upload_status: string; bytes_received: number; fingerprint: string | null; fingerprint_kind?: string | null; thumb_status: 'pending' | 'running' | 'done' | 'failed' | null; thumb_error: string | null }
export interface ProductTag { value: string; terms: string[]; source: string; ai_value: string; flag: string | null }
export interface BatchProduct { id: number; batch_file_id: number; kind: 'set' | 'item'; display_name: string; ai_status: string; ai_error: string | null; tags: Record<string, ProductTag>; free_text: string; flags: string[]; relation_items: { batch_product_id: number; source: string }[]; relation_confirmed: boolean }
export interface AiProgress { items_total: number; items_done: number; sets_total: number; sets_done: number; failed: number }
export interface Batch { id: number; batch_no: string; folder_name: string; original_folder_name: string; project_code: string; status: string; queued: boolean; won: boolean; terminated_at?: string | null; termination_reason?: string | null; updated_at: string; files: UploadFile[]; directories: string[]; chunk_bytes: number; parallel_files: number; upload_token: string | null; differences: Record<string, string[]> | null; owner_id?: number; uploader_id?: number; recommended_path?: string | null; ai_progress?: AiProgress; products?: BatchProduct[]; tag_labels?: Record<string, string> }
export interface Precheck { project_code: string; company_name: string; project_name: string; code_status: 'new' | 'exists' | 'need_h_code'; issues: Issue[]; path_prefix_length?: number }
export interface SelectionBody { folder_name: string; files: ManifestEntry[] }
const headers = (token?: string | null): Record<string, string> => token ? { 'X-Upload-Token': token } : {}
export const checkUpload = (body: SelectionBody, resumeId?: number | null): Promise<Precheck> => request(`/api/batches/precheck${resumeId ? `?resume_id=${resumeId}` : ''}`, { method: 'POST', body: JSON.stringify(body) })
export const createBatch = (body: SelectionBody & { won: boolean; historical_year?: number; acks: string[] }): Promise<Batch> => request('/api/batches', { method: 'POST', body: JSON.stringify({ ...body, kind: 'ingest' }) })
export const getBatches = (): Promise<Batch[]> => request('/api/batches')
export const getBatch = (id: number): Promise<Batch> => request(`/api/batches/${id}`)
export const getUploadState = (id: number, token?: string | null): Promise<Batch> => request(`/api/batches/${id}/upload-state`, { headers: headers(token) })
export const resumeBatch = (id: number, body: SelectionBody & { acks: string[]; accept_changes: boolean }): Promise<Batch> => request(`/api/batches/${id}/resume`, { method: 'POST', body: JSON.stringify(body) })
export const sendChunk = (id: number, file: number, index: number, data: Blob, token: string, signal: AbortSignal): Promise<{ bytes_received: number }> => request(`/api/batches/${id}/files/${file}/chunks/${index}`, { method: 'PUT', body: data, headers: { ...headers(token), 'Content-Type': 'application/octet-stream' }, signal })
export const completeFile = (id: number, file: number, token: string): Promise<Batch> => request(`/api/batches/${id}/files/${file}/complete`, { method: 'POST', headers: headers(token) })
export const pauseBatch = (id: number, token?: string | null, keepalive = false): Promise<Batch> => request(`/api/batches/${id}/pause`, { method: 'POST', headers: headers(token), keepalive })
export const terminateBatch = (id: number, reason: string): Promise<Batch> => request(`/api/batches/${id}/cancel`, { method: 'POST', body: JSON.stringify({ reason }) })
export const confirmBatch = (id: number): Promise<Batch> => request(`/api/batches/${id}/confirm`, { method: 'POST' })
export const retryAi = (id: number): Promise<Batch> => request(`/api/batches/${id}/retry-ai`, { method: 'POST' })
