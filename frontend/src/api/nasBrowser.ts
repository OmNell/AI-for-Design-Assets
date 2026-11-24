import { request, requestResponse } from './client'

export type BrowseKind = 'all' | 'folder' | 'image' | 'file'
export type BrowseSort = 'name' | 'size' | 'modified'
export interface NasEntry {
  path: string
  name: string
  kind: Exclude<BrowseKind, 'all'>
  is_dir: boolean
  extension: string
  size: number
  modified_at: string
}
interface NasInfo extends NasEntry { exists: boolean; accessible: boolean; catalog_status: 'unmanaged' | 'unverified' }
interface NasDirectory {
  root_name: string
  path: string
  accessible: boolean
  folders: NasEntry[]
  files: NasEntry[]
  page: number
  page_size: number
  total: number
}
export interface BrowseFilters { page: number; kind: BrowseKind; sort: BrowseSort; q: string }

const endpoint = (operation: string, parameters: Record<string, string>) =>
  '/api/nas-browser/' + operation + '?' + new URLSearchParams(parameters)

export const getNasDirectory = (path: string, filter: BrowseFilters, signal?: AbortSignal) =>
  request<NasDirectory>(endpoint('list', { ...filter, path, page: String(filter.page) }), { signal })
export const getNasInfo = (path: string, signal?: AbortSignal) => request<NasInfo>(endpoint('info', { path }), { signal })
export const nasPreviewUrl = (path: string, size: 300 | 1600) => endpoint('preview', { path, size: String(size) })
export const nasDownloadUrl = (path: string) => {
  const transfer = Array.from(crypto.getRandomValues(new Uint8Array(12)),
    byte => byte.toString(16).padStart(2, '0')).join('')
  return endpoint('download', { path, transfer })
}
export const getNasPreview = (path: string, signal: AbortSignal) =>
  requestResponse(nasPreviewUrl(path, 1600), { signal }).then(response => response.blob())
