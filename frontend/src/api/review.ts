import type { OperationKind } from './operations'
import { request } from './client'
import type { DescriptionState, ProductTag } from './upload'

export interface ReviewProduct {
  id: number; batch_file_id: number; kind: 'set' | 'item'; display_name: string; file_name: string
  fingerprint: string | null; thumb_status: string | null; thumb_error: string | null
  ai_status: 'pending' | 'done' | 'failed' | 'awaiting_description'; ai_error: string | null; visual_ai_succeeded: boolean
  tags: Record<string, ProductTag>; free_text: string; free_text_source: string; ai_free_text: string
  pre_description?: string | null; pre_description_state?: DescriptionState; ai_raw_result?: string | null
  flags: string[]; relation_items: { batch_product_id: number; source: string }[]; relation_confirmed: boolean; relation_confirm_kind: "pending" | "confirmed_members" | "confirmed_empty"
}
export interface ReviewField { key: string; label: string; hint: string }
export interface Pending { fields_unconfirmed: number; invalid_files: number; missing_main_image: boolean; yellow: number; relations_unconfirmed: number; failed: number; processing: number; can_submit: boolean }
export interface FolderGroup { name: string; files: { name: string; size: number }[] }
export interface ReviewBatch {
  id: number; kind?: OperationKind | 'ingest'; project_id?: number | null; batch_no: string; project_code: string; folder_name: string; company_name: string; project_name: string
  status: string; won: boolean; round: number; owner_id: number; owner_name: string; uploader_id: number
  recommended_path: string | null; note_to_reviewer: string | null; editable: boolean; review_snapshot_hash: string | null
}
export interface PinView { batch_product_id: number; product_name: string; x: number; y: number; text: string }
export interface RoundView { round: number; stage: string; reviewer_name: string; decision: string; comment: string | null; decided_at: string | null; pins: PinView[] }
export interface ReviewData { history?: RoundView[]; batch: ReviewBatch; fields: ReviewField[]; products: ReviewProduct[]; pending: Pending; folders: FolderGroup[] }
export interface ProductUpdate { product: ReviewProduct; pending: Pending }
export interface ProductPatch { tags?: Record<string, string>; free_text?: string; confirmations?: Record<string, "confirmed_value" | "confirmed_unknown">; resolve_flags?: string[] }
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
  request(`${base(id)}/products/${pid}/relations`, { method: 'PUT', body: JSON.stringify({ item_ids, confirm_kind: item_ids.length ? "confirmed_members" : "confirmed_empty" }) })
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
