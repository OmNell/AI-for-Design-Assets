export type Role = 'uploader' | 'first_reviewer' | 'final_reviewer' | 'admin'
export interface User { id: number; username: string; display_name: string | null; roles: Role[]; enabled: boolean }
export const roleLabels: Record<Role, string> = { uploader: '上传人', first_reviewer: '初审', final_reviewer: '终审', admin: '管理员' }
