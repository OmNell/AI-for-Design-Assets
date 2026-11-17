import { request } from './client'
import type { RoundView } from './review'

export interface ChangeRow { label: string; before: string; after: string }
export interface ChangePreview { kind: string; kind_label: string; reason: string | null; rows: ChangeRow[]; products: string[]; files: string[]; notes: string[] }
export interface OpProgress { step: number; total: number; current: string | null; error: string | null; can_resume: boolean }
export interface OpDetail { id: number; batch_no: string; kind: string; kind_label: string; status: string; project_id: number | null; project_code: string; folder_name: string
  owner_id: number; owner_name: string; round: number; created_at: string; preview: ChangePreview | null; progress: OpProgress | null; can_withdraw: boolean; history: RoundView[] }
export interface PurgeTargets { history_file_ids: number[]; product_ids: number[]; project: boolean }
export interface PurgePreview { files: { path: string; size: number }[]; count: number; total_size: number; confirm_label: string; cooling_days: number; in_cooling: boolean; earliest_allowed: string | null; summary: string[] }

const post = <T>(url: string, body?: unknown): Promise<T> => request(url, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
const op = (projectId: number, name: string, body?: unknown): Promise<OpDetail> => post(`/api/projects/${projectId}/ops/${name}`, body)

export const renameProject = (id: number, body: { project_name: string | null; products: { id: number; name: string }[] }) => op(id, 'rename', body)
export const retagProject = (id: number) => op(id, 'retag')
export const voidItems = (id: number, body: { product_ids: number[]; file_ids: number[]; reason: string; cascade: boolean }) => op(id, 'void', body)
export const voidProject = (id: number, reason: string) => op(id, 'void-project', { reason })
export const restoreItems = (id: number, body: { product_ids: number[]; file_ids: number[]; project: boolean; reason: string }) => op(id, 'restore', body)
export const moveProject = (id: number, body: { won: boolean; company_id: number; reason: string }) => op(id, 'move', body)
export const previewPurge = (id: number, targets: PurgeTargets): Promise<PurgePreview> => post(`/api/projects/${id}/ops/purge/preview`, targets)
export const purge = (id: number, body: PurgeTargets & { reason: string; exception_reason: string; confirm: string }) => op(id, 'purge', body)
export const getOp = (batchId: number): Promise<OpDetail> => request(`/api/ops/${batchId}`)
export const withdrawOp = (batchId: number): Promise<OpDetail> => post(`/api/ops/${batchId}/withdraw`)
