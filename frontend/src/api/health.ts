export interface Health {
  version: string
  mode: 'dev' | 'prod'
  database_available: boolean
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  const response = await fetch('/api/health', { signal })
  if (!response.ok) throw new Error('暂时无法获取运行状态，请稍后重试。')
  const data: unknown = await response.json()
  if (typeof data !== 'object' || data === null || !('version' in data) ||
      typeof data.version !== 'string' || !('mode' in data) ||
      (data.mode !== 'dev' && data.mode !== 'prod') || !('database_available' in data) ||
      typeof data.database_available !== 'boolean') {
    throw new Error('服务返回的状态格式不正确。')
  }
  return {
    version: data.version, mode: data.mode, database_available: data.database_available,
  }
}
