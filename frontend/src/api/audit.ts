import { request } from './client'
import type { FrozenOperation, OperationKind } from './operations'
import type { ReviewData, RoundView, PinView } from './review'

export interface Industry { id?: number; name: string; seq?: number | null }
export interface Company { id?: number; name: string; aliases: string[]; industry_id?: number | null; industry_name?: string | null; seq?: number | null }
export interface CompanyMap { industries: Industry[]; companies: Company[] }
export interface TreeRoot { won: boolean; folder: string; industries: { id: number; name: string; folder: string; companies: { id: number; name: string; folder: string }[] }[] }
export interface Person { id: number; name: string }
export interface QueueItem { id: number; kind?: OperationKind | 'ingest'; batch_no: string; folder_name: string; round: number; uploader_name: string; products: number; files: number; total_size: number
  waiting_since: string; waiting_seconds: number; claimed_by: Person | null; claimed_by_me: boolean; blocked: boolean }
export interface PathInfo { recommended: string | null; final: string | null; effective: string | null; company_name: string; company_in_tree: boolean }
export interface AuditData { version: string; operation?: FrozenOperation | null;
  review: ReviewData; stage: 'first' | 'final' | null; claimed_by: Person | null; claimed_by_me: boolean; can_claim: boolean; blocked: boolean; can_edit_path: boolean
  path: PathInfo; acks: string[]; stats: { manual_edits: number; ai_confirmed: number; confirmed_unknown: number; appended: number; removed: number }
  relations: { set_id: number; set_name: string; items: string[]; ai_count: number; manual_count: number; confirmed: boolean }[]
  note: string | null; folder_counts: { name: string; count: number }[]; history: RoundView[]; pins: PinView[]; checklist: Record<string, string>
}
export interface PinInput { batch_product_id: number; x: number; y: number; text: string }
export interface Todo { key: string; label: string; count: number; link: string; batches: { id: number; label: string }[] }
export type Stage = 'first' | 'final'

const json = (method: string, body?: unknown): RequestInit => ({ method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
export const getCompanyMap = (): Promise<CompanyMap> => request('/api/admin/company-map')
export const saveCompanyMap = (body: CompanyMap): Promise<CompanyMap> => request('/api/admin/company-map', json('PUT', body))
export const getCatalogTree = (): Promise<TreeRoot[]> => request('/api/catalog/tree')
export const getTodos = (): Promise<Todo[]> => request('/api/todos')
export const getQueue = (stage: Stage): Promise<QueueItem[]> => request(`/api/review/queue?stage=${stage}`)
export const getAudit = (id: number): Promise<AuditData> => request(`/api/review/${id}`)
const write = (version: string, method: string, body: unknown): RequestInit => ({
  ...json(method, body), headers: {'X-Review-Version': version},
})
export const claimAudit = (id: number, version: string): Promise<AuditData> => request(`/api/review/${id}/claim`, {...json('POST'), headers: {'X-Review-Version':version}})
export const releaseAudit = (id: number, version: string): Promise<AuditData> => request(`/api/review/${id}/release`, {...json('POST'), headers: {'X-Review-Version':version}})
export const savePins = (id: number, version: string, pins: PinInput[]): Promise<AuditData> => request(`/api/review/${id}/pins`, write(version, 'PUT', { pins }))
export const changePath = (id: number, version: string, won: boolean, company_id: number): Promise<AuditData> => request(`/api/review/${id}/path`, write(version, 'PUT', { won, company_id }))
export const passAudit = (id: number, version: string, checklist: Record<string, boolean>): Promise<AuditData> => request(`/api/review/${id}/pass`, write(version, 'POST', { checklist }))
export const rejectAudit = (id: number, version: string, comment: string, pins: PinInput[]): Promise<AuditData> => request(`/api/review/${id}/reject`, write(version, 'POST', { comment, pins }))
