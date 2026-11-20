import { request } from './client'

export interface NasTarget { host: string; dsm_port: number; share: string }
export interface NasPublic extends NasTarget {
  work_root: string | null; system_user: string; has_system_password: boolean; regions: Record<string, string>
  certificate: 'trusted_ca' | 'system_ca' | 'legacy_http'
}
export interface NasStatus {
  state: 'active' | 'pending_restart' | 'failed'; revision: string | null; active: NasPublic
  pending: NasPublic | null; message: string; mode: 'dev' | 'prod'; connection_checks: NasCheck[]
}
export interface NasCandidate extends NasTarget {
  work_root: string; system_user: string; actor_password: string; system_password?: string
}
export interface NasCheck { name: string; state: 'passed' | 'failed' | 'manual'; message: string }
export interface NasValidation {
  eligible: boolean; checks: NasCheck[]; changes: string[]; conflicts: string[]; regions: Record<string, string>
}
interface NasNodes { path: string; nodes: string[]; truncated: boolean }
interface NasTest extends NasTarget {
  protocol: 'dsm' | 'smb'; actor_password: string; work_root?: string; username?: string; permission_profile?: 'employee' | 'final'
}

const base = '/api/admin/nas'
const post = <T>(action: string, data: unknown) => request<T>(`${base}/${action}`, { method: 'POST', body: JSON.stringify(data) })
export const getNasConfig = () => request<NasStatus>(`${base}/config`)
export const getNasStatus = () => request<NasStatus>(`${base}/status`)
export const testNas = (data: NasTest) => post<NasCheck[]>('test', data)
export const browseNas = (target: NasTarget, path: string, actor_password: string) => post<NasNodes>('browse', { ...target, path, actor_password })
export const validateNas = (data: NasCandidate) => post<NasValidation>('validate', data)
export const applyNas = (data: NasCandidate, revision: string) => post<NasStatus>('apply', { ...data, revision, confirmed: true })
