import { request } from './client'
import type { ProductTag } from './upload'

export interface ReviewProduct {
  id: number; batch_file_id: number; kind: 'set' | 'item'; display_name: string; file_name: string
  fingerprint: string | null; thumb_status: string | null; thumb_error: string | null
  ai_status: 'pending' | 'done' | 'failed'; ai_error: string | null; manually_filled: boolean
  tags: Record<string, ProductTag>; free_text: string; free_text_source: string; ai_free_text: string
  flags: string[]; relation_items: { batch_product_id: number; source: string }[]; relation_confirmed: boolean
}
export interface ReviewField { key: string; label: string; hint: string }
export interface Pending { yellow: number; relations_unconfirmed: number; failed: number; processing: number; can_submit: boolean }
export interface FolderGroup { name: string; files: { name: string; size: number }[] }
export interface ReviewBatch {
  id: number; batch_no: string; project_code: string; folder_name: string; company_name: string; project_name: string
  status: string; won: boolean; round: number; owner_id: number; owner_name: string; uploader_id: number
  recommended_path: string | null; note_to_reviewer: string | null; editable: boolean
}
export interface ReviewData { batch: ReviewBatch; fields: ReviewField[]; products: ReviewProduct[]; pending: Pending; folders: FolderGroup[] }
export interface ProductUpdate { product: ReviewProduct; pending: Pending }
export interface ProductPatch { tags?: Record<string, string>; free_text?: string; confirm_fields?: string[]; resolve_flags?: string[] }
export interface TagValue { term: string; count: number }

const base = (id: number) => `/api/batches/${id}`
export const getReview = (id: number): Promise<ReviewData> => request(`${base(id)}/products`)
export const patchProduct = (id: number, pid: number, body: ProductPatch, keepalive = false): Promise<ProductUpdate> =>
  request(`${base(id)}/products/${pid}`, { method: 'PATCH', body: JSON.stringify(body), keepalive })
export const bulkUpdate = (id: number, product_ids: number[], tags: Record<string, string>): Promise<ReviewData> =>
  request(`${base(id)}/products/bulk`, { method: 'POST', body: JSON.stringify({ product_ids, tags }) })
export const getTagValues = (field: string, q: string, batchId: number, signal?: AbortSignal): Promise<TagValue[]> =>
  request(`/api/tag-values?field=${encodeURIComponent(field)}&q=${encodeURIComponent(q)}&batch_id=${batchId}`, { signal })
export const retryProductAi = (id: number, pid: number): Promise<ProductUpdate> => request(`${base(id)}/products/${pid}/retry-ai`, { method: 'POST' })
export const setRelations = (id: number, pid: number, item_ids: number[]): Promise<ProductUpdate> =>
  request(`${base(id)}/products/${pid}/relations`, { method: 'PUT', body: JSON.stringify({ item_ids }) })
export function appendProduct(id: number, kind: 'set' | 'item', file: File, filename: string): Promise<ReviewData> {
  const form = new FormData()
  form.append('kind', kind); form.append('filename', filename); form.append('mtime', String(Math.max(0, Math.round(file.lastModified))))
  form.append('file', file, filename)
  return request(`${base(id)}/products`, { method: 'POST', body: form })
}
export const removeProduct = (id: number, pid: number): Promise<ReviewData> => request(`${base(id)}/products/${pid}`, { method: 'DELETE' })
export const submitReview = (id: number, note: string): Promise<ReviewData> => request(`${base(id)}/submit`, { method: 'POST', body: JSON.stringify({ note }) })
export const changeOwner = (id: number, user_id: number): Promise<{ id: number; owner_id: number; owner_name: string }> =>
  request(`/api/admin/batches/${id}/owner`, { method: 'PUT', body: JSON.stringify({ user_id }) })
