import { request, requestResponse } from './client'

export interface ProjectRow {
  id: number; project_code: string; project_name: string; company_name: string; industry_name: string
  won: boolean; status: string; ingested_at: string | null; link_url: string | null; products: number; files: number
}

export interface ProductBrief {
  id: number; product_code: string; display_name: string; kind: 'item' | 'set'; status: string
  link_id: string | null; link_url: string | null; related: number[]; tags: string[]
}
export interface ProjectDetail {
  project: ProjectRow; folder_name: string; products_list: ProductBrief[]
  folders: { name: string; count: number }[]
  history: { id: number; file_id: number; old_rel_path: string; new_rel_path: string; reason: string; created_at: string }[]
  logs: { ts: string; user: string; action: string; result: string }[]
}

export interface ProjectQuery { q: string; won: '' | 'true' | 'false'; industry: string; company: string; page: number; sort: string }
export interface ProjectPage { items: ProjectRow[]; total: number; page: number; page_size: number }
export interface FileRow {
  id: number; name: string; rel_path: string; size: number; mtime: number; status: string; category: string
  link_url: string | null; is_image: boolean
}
export interface ProductDetail {
  id: number; product_code: string; display_name: string; kind: 'item' | 'set'; status: string
  free_text: string; file_id: number; product_url: string | null; project: ProjectRow; related: ProductBrief[]
  tags: { key: string; label: string; value: string; terms: string[]; source: string; confirmation_status: string }[]
}
export interface FileCheck { id: number; name: string; size: number; status: string; is_image: boolean; warning: string | null; url: string }
export interface DownloadItem { file_id: number; name: string | null; size: number | null; url: string | null; error: string | null; code: string | null }
export interface DownloadProgress { status: 'pending' | 'sending' | 'sent' | 'interrupted' | 'failed'; message: string | null }
export interface ResolvedLink {
  link_id: string; kind: 'file' | 'product' | 'project'; target_id: number; project_id: number
  url: string; status: string; image_file_id: number | null; deleted_at?: string | null; delete_reason?: string | null
}
export interface AssetFinding { id: number; project_id: number; file_id: number; kind: string; detail: string; created_at: string }

export function getProjects({ q, won, industry, company, page, sort }: ProjectQuery): Promise<ProjectPage> {
  const params = new URLSearchParams({ q, industry, company, page: String(page), sort })
  if (won) params.set('won', won)
  return request(`/api/projects?${params}`)
}
export const getProject = (id: number): Promise<ProjectDetail> => request(`/api/projects/${id}`)
export const getProjectFiles = (id: number, category?: string): Promise<FileRow[]> => request(`/api/projects/${id}/files${category ? `?category=${encodeURIComponent(category)}` : ''}`)
export const getProduct = (id: number): Promise<ProductDetail> => request(`/api/products/${id}`)
export const getLink = (id: string): Promise<ResolvedLink> => request(`/api/l/${encodeURIComponent(id)}`)
export const checkFile = (id: number): Promise<FileCheck> => request(`/api/files/${id}/check`)
export const downloadMany = (fileIds: number[]): Promise<DownloadItem[]> => request('/api/files/download-many', { method: 'POST', body: JSON.stringify({ file_ids: fileIds }) })
export const getDelivery = (id: number, transfer: string): Promise<DownloadProgress> => request(`/api/files/${id}/downloads/${encodeURIComponent(transfer)}`)
export const getFindings = (): Promise<AssetFinding[]> => request('/api/admin/asset-findings')
export const thumbUrl = (id: number) => `/api/thumbs/${id}/300`
export const getPreview = (url: string, signal: AbortSignal): Promise<Response> => requestResponse(url, { signal })
