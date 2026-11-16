import { useAuthStore } from '../stores/auth'

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail: unknown = null) { super(message) }
}
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  try { response = await fetch(path, { ...options, credentials: 'same-origin',
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } }) }
  catch (error: unknown) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, 'network_error', '无法连接工作台，请检查网络后重试')
  }
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const body = typeof data === 'object' && data !== null ? data : {}
    const code = 'code' in body && typeof body.code === 'string' ? body.code : 'request_failed'
    const message = code === 'nas_session_expired' ? 'NAS 连接已超时，请重新登录' :
      'message' in body && typeof body.message === 'string' ? body.message : '请求未能完成，请稍后重试'
    if (response.status === 401) useAuthStore.getState().clear(path === '/api/auth/login' ? '' : message)
    throw new ApiError(response.status, code, message, 'detail' in body ? body.detail : null)
  }
  if (data === null) throw new ApiError(response.status, 'invalid_response', '工作台响应格式不正确，请稍后重试')
  return data as T
}
