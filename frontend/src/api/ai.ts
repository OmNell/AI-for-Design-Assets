import { request } from './client'

export interface AiSettings {
  base_url: string
  api_key_set: boolean
  model_vision: string
  model_text: string
  model_embed: string
  concurrency: number
  timeout_s: number
  retries: number
  stall_alert_min: number
}
export type AiSettingsInput = Omit<AiSettings, 'api_key_set'> & { api_key?: string }
export interface ModelTest { kind: string; model: string; success: boolean; error: string | null }
export interface TagField { key: string; label: string; hint: string; sort_order: number; enabled: boolean }
export interface TagFields { version: number; fields: TagField[] }
export interface AiAlert { id: number; batch_id: number; run_id: number; reason: string; detected_at: string }

export const getAiSettings = (): Promise<AiSettings> => request('/api/settings/ai')
export const saveAiSettings = (value: AiSettingsInput): Promise<AiSettings> => request('/api/settings/ai', { method: 'PUT', body: JSON.stringify(value) })
export const testAiModels = (): Promise<{ results: ModelTest[] }> => request('/api/settings/ai/test', { method: 'POST' })
export const getTagFields = (): Promise<TagFields> => request('/api/admin/tag-fields')
export const saveTagFields = (value: TagFields): Promise<TagFields> => request('/api/admin/tag-fields', { method: 'PUT', body: JSON.stringify(value) })
export const getAiAlerts = (): Promise<AiAlert[]> => request('/api/admin/ai-alerts')
