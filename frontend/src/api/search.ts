import { request } from './client'

export interface Condition { type: 'field' | 'exclude' | 'other' | 'company'; field: string | null; label: string; terms: string[] }
export interface SearchFilters { won?: boolean | null; industry?: number | null; company?: number | null }
export interface SearchResult {
  id: number; product_code: string; display_name: string; kind: 'set' | 'item'; tags: { key: string; label: string; value: string }[]; free_text: string
  related: { id: number; product_code: string; display_name: string }[]
  project: { id: number; project_code: string; project_name: string; company_name: string; won: boolean; link_id: string | null }
  link_id: string | null; reason: string | null; matched: string[]; by_keywords: boolean; by_vector: boolean; similarity: number | null
}
export interface ProjectHit { id: number; project_code: string; project_name: string; company_name: string; won: boolean; link_id: string | null }
export interface SearchResponse {
  log_id: number; query: string; kind: 'code' | 'natural'; conditions: Condition[]; relaxed: boolean; degraded: boolean; notes: string[]; from_cache: boolean
  model_calls: number; elapsed_ms: number; local_ms: number; total: number; page: number; page_size: number; results: SearchResult[]; project_hits: ProjectHit[]
}
export interface IndexStatus { backend: string; model: string; products: number; vectors: number; dim: number | null; queued: number }

export const search = (query: string, filters: SearchFilters, conditions?: Condition[], page = 1): Promise<SearchResponse> =>
  request('/api/search', { method: 'POST', body: JSON.stringify({ query, filters, page, ...(conditions ? { conditions } : {}) }) })
export const recordClick = (log_id: number, product_id: number): Promise<{ message: string }> =>
  request('/api/search/click', { method: 'POST', body: JSON.stringify({ log_id, product_id }), keepalive: true })
export const getIndexStatus = (): Promise<IndexStatus> => request('/api/admin/search-index')
export const rebuildIndex = (): Promise<IndexStatus> => request('/api/admin/search-index/rebuild', { method: 'POST' })
