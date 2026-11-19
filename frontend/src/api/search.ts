import { request, requestResponse } from './client'
import type { ProjectRow } from './catalog'

export interface SearchFilters { won: boolean | null; industry: string; company: string }
export interface SearchInput extends SearchFilters { query: string; remove: string[] }

export interface SearchHit {
  id: number; project_id: number; product_code: string; name: string; kind: 'item' | 'set'
  project_name: string; project_code: string; company_name: string; won: boolean; project_url: string | null; url: string | null; tags: Record<string, string>
  related: string[]; matches: string[]; keyword_score: number; similarity: number | null; reason: string
}
export interface SearchResult {
  search_id: number; products: SearchHit[]; projects: ProjectRow[]
  conditions: { fields: Record<string, string[]>; keywords: string[]; exclude: string[] }
  warnings: string[]; degraded: boolean; exact: boolean; permission_filtered: boolean
  field_labels: Record<string, string>; index_generation: number | null
}
export interface IndexInfo {
  generation: number; name: string; version: string; revision: string; status: 'formal' | 'candidate' | 'rollback'
  dimension: number | null; total: number; ready: number; failures: number; pending: number
  verified: boolean; running: boolean
}
export interface IndexStatus { backend: string; indexes: IndexInfo[] }
export type IndexAction = 'switch' | 'retry' | 'rollback' | 'cancel'

const json = (body: unknown): RequestInit => ({ method: 'POST', body: JSON.stringify(body) })
export const searchAssets = (input: SearchInput): Promise<SearchResult> => request('/api/search', json(input))
export async function searchClick(searchId: number, kind: 'project' | 'product', id: number) {
  await requestResponse('/api/search/click', { ...json({ search_id: searchId, kind, id }), keepalive: true })
}
export const getIndexes = (): Promise<IndexStatus> => request('/api/admin/vector-indexes')
export const buildIndex = (name: string, revision: string): Promise<IndexStatus> => request('/api/admin/vector-indexes', json({ name, revision }))
export const changeIndex = (id: number, action: IndexAction): Promise<IndexStatus> => request(`/api/admin/vector-indexes/${id}/${action}`, { method: 'POST' })
