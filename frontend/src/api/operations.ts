import { request } from './client'
import type { ReviewData, ReviewField } from './review'
import type { Batch, Precheck, ProductTag, SelectionBody } from './upload'

export const operationLabels = {
  supplement: '补充资料或替换主图',
  rename: '改名',
  retag: '修改标签、描述或关系',
  void: '作废',
  restore: '恢复',
  move: '移动项目',
  purge: '物理删除',
  archive_repair: '档案修复',
}
export type OperationKind = keyof typeof operationLabels
export type LightOperation = Exclude<OperationKind, 'supplement'>
export interface OperationProduct {
  id: number; product_code: string; display_name: string; kind: 'item' | 'set'; status: string; file_id: number
  free_text?: string; tags?: Record<string, ProductTag>; related?: number[]; voided_at?: string | null; purge_eligible_at?: string | null
}
export interface OperationFile {
  id: number; rel_path: string; physical_rel_path?: string; mtime?: number; size: number; fingerprint: string | null; status: string
  voided_at?: string | null; purge_eligible_at?: string | null
}
export interface OperationManifest {
  project: { id: number; project_code: string; project_name: string; company_name: string; won: boolean; status: string
    folder_name?: string; current_path?: string; company_id?: number | null; industry_id?: number | null; voided_at?: string | null; purge_eligible_at?: string | null }
  products: OperationProduct[]; files: OperationFile[]
  tags?: (ProductTag & { product_id: number; field_key: string })[]
  relations?: { set_product_id: number; item_product_id: number; status: string }[]
  history?: { id: number; file_id: number; product_id: number | null; old_rel_path: string; new_rel_path: string }[]
  fields?: ReviewField[]
}
export interface ProjectOperations {
  before: OperationManifest
  kinds: OperationKind[]
  project_id: number; project_code: string; folder_name: string; status: string
  active_batch_id?: number | null
}
export interface RetagPlan {
  id: number; name?: string; free_text?: string; tags?: Record<string, { value: string; confirmation_status: 'confirmed_value' | 'confirmed_unknown' }>
  item_ids?: number[]
}
export interface OperationInput {
  project_name?: string
  products?: { id: number; name?: string; rename_file?: boolean }[] | RetagPlan[] | { id: number; existing_product_id: number | null }[]
  project?: boolean; product_ids?: number[]; file_ids?: number[]
  reason?: string; confirm?: string; company_id?: number; won?: boolean
  files?: { id: number; choice: 'replace' | 'keep_both' }[]
  relation_reviews?: Record<string, number[]>
  batch_set_items?: Record<string, number[]>
  relation_batch_items?: Record<string, number[]>
}
export interface SupplementFile {
  id: number; rel_path: string; fingerprint?: string | null; conflict: 'new' | 'duplicate' | 'conflict' | null
  file_id?: number | null; choice?: 'replace' | 'keep_both' | null
}
export interface PurgeFile {
  file_id: number; path: string; size: number; fingerprint: string; history: boolean
}
export interface PurgePreview {
  confirmation_code: string
  purge_files: PurgeFile[]
  product_codes?: string[]
  relationships?: string[]
}
export interface DraftPurgePreview {
  project_code: string; confirm_label: string; eligible: boolean; earliest_allowed: string
  files: (Omit<PurgeFile, 'history'> & { historical?: boolean })[]
  products: { id: number; product_code: string }[]
  history: { file_id: number }[]
  relations: { set_product_id: number; item_product_id: number }[]
}
export type SnapshotValue = string | number | boolean | null | Record<string, string | number | boolean | null>
export type OperationChanges = Partial<Record<'project' | 'files' | 'products' | 'tags' | 'relations', Record<string, { before: SnapshotValue; after: SnapshotValue }>>>
export interface FrozenOperation {
  kind: OperationKind; before: OperationManifest; after?: OperationChanges | null
  params: OperationInput; purge?: DraftPurgePreview | null
}
export interface OperationData extends FrozenOperation {
  batch_id: number; project_id: number; status: string; editable: boolean; round?: number; history?: ReviewData['history']
  conflicts: SupplementFile[]; blockers?: string[]; fields?: ReviewField[]; required_relation_reviews?: number[]
}
export interface OperationExecution extends Omit<PurgePreview, 'confirmation_code'> {
  status: string; step: number; step_name: string; error: string | null; can_resume: boolean
  confirmation_code: string | null
}

const base = (id: number) => `/api/batches/${id}/operation`
const json = (method: string, content: unknown) => ({ method, body: JSON.stringify(content) })
export const getOperationOptions = (project: number): Promise<ProjectOperations> => request(`/api/projects/${project}/operations`)
export const startOperation = (project: number, kind: LightOperation, content: OperationInput = {}): Promise<OperationData> =>
  request(`/api/projects/${project}/ops/${kind}`, json('POST', { params: content }))
export const checkSupplement = (project: number, content: SelectionBody): Promise<Precheck> =>
  request(`/api/projects/${project}/ops/supplement/precheck`, json('POST', content))
export const createSupplement = (project: number, content: SelectionBody & { won: boolean; acks: string[] }): Promise<Batch> =>
  request(`/api/projects/${project}/ops/supplement`, json('POST', content))
export const getOperation = (id: number): Promise<OperationData> => request(base(id))
export const updateOperation = (id: number, content: OperationInput): Promise<OperationData> => request(base(id), json('PUT', { params: content }))
export const previewOperation = (id: number): Promise<OperationData> => request(`${base(id)}/preview`)
export const submitOperation = (id: number, params: OperationInput, note: string): Promise<ReviewData> => request(`${base(id)}/submit`, json('POST', { params, note }))
export const getExecution = (id: number): Promise<OperationExecution> => request(`/api/operations/${id}/execution`)
export const executeOperation = (id: number, confirm_code?: string): Promise<OperationExecution> =>
  request(`/api/operations/${id}/execute`, json('POST', { confirm_code }))

export const operationHref = (id: number, kind: OperationKind | 'ingest' | undefined, status: string) =>
  ['上传中', '上传中断'].includes(status) ? `/upload?resume=${id}`
    : ['待核对', '已驳回'].includes(status) ? `/batches/${id}/${kind && !['ingest', 'supplement'].includes(kind) ? 'operation' : 'review'}`
      : `/batches/${id}`

export const getVoidedProjects = (): Promise<ProjectOperations[]> => request('/api/operations/voided-projects')
