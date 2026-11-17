import { request } from './client'

export interface ProjectRow { id: number; project_code: string; project_name: string; company_name: string; industry_name: string; won: boolean; current_path: string; status: string; ingested_at: string | null; products: number; files: number }
export interface ProjectList { items: ProjectRow[]; total: number; page: number; page_size: number }
export interface ProductBrief { id: number; product_code: string; display_name: string; kind: 'set' | 'item'; status: string; link_id: string | null; related: number[]; file_id: number | null; lifecycle: Record<string, unknown> }
export interface HistoryRow { id: number; file_id: number; product_id: number | null; product_code: string | null; old_rel_path: string; new_rel_path: string; reason: string; created_at: string; file_status: string; size: number; link_id: string | null }
export interface OpenBatch { id: number; batch_no: string; kind: string; kind_label: string; status: string }
export interface ProjectDetail { project: ProjectRow; folder_name: string; code_source: string; link_id: string | null; products_list: ProductBrief[]
  folders: { name: string; count: number }[]; history: HistoryRow[]
  logs: { ts: string; user: string; action: string; result: string }[]; open_batch: OpenBatch | null; lifecycle: Record<string, unknown> }
export interface FileRow { id: number; name: string; rel_path: string; size: number; mtime: number; status: string; category: string; link_id: string | null }
export interface ProductDetail { id: number; product_code: string; display_name: string; kind: 'set' | 'item'; status: string; free_text: string; file_id: number; image_rel_path: string
  tags: { key: string; label: string; value: string; terms: string[]; source: string }[]; related: ProductBrief[]; project: ProjectRow; product_link: string | null; project_link: string | null; ingested_at: string }
export interface LinkTarget { link_id: string; kind: 'product' | 'file' | 'project'; target_id: number; status: string; url: string | null; message: string | null }
export interface ProjectQuery { q: string; won: '' | 'true' | 'false'; industry: string; company: string; page: number }

export function getProjects(query: ProjectQuery): Promise<ProjectList> {
  const params = new URLSearchParams({ page: String(query.page) })
  if (query.q.trim()) params.set('q', query.q.trim())
  if (query.won) params.set('won', query.won)
  if (query.industry) params.set('industry', query.industry)
  if (query.company) params.set('company', query.company)
  return request(`/api/projects?${params}`)
}
export const getProject = (id: number): Promise<ProjectDetail> => request(`/api/projects/${id}`)
export const getProjectFiles = (id: number, category: string): Promise<FileRow[]> => request(`/api/projects/${id}/files?category=${encodeURIComponent(category)}`)
export const getProduct = (id: number): Promise<ProductDetail> => request(`/api/products/${id}`)
export const getLink = (linkId: string): Promise<LinkTarget> => request(`/api/l/${encodeURIComponent(linkId)}?format=json`)
export const checkFile = (id: number): Promise<{ id: number; name: string; size: number }> => request(`/api/files/${id}/check`)
export const downloadMany = (file_ids: number[]): Promise<{ file_id: number; name: string; size: number; url: string }[]> =>
  request('/api/files/download-many', { method: 'POST', body: JSON.stringify({ file_ids }) })
export const thumbUrl = (productId: number, size: 300 | 1024 | 1600) => `/api/thumbs/${productId}/${size}`
export const linkUrl = (linkId: string) => `${window.location.origin}/l/${linkId}`
