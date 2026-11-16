import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { listUsers, updateUser } from '../api/roles'
import { getMe } from '../api/auth'
import { roleLabels, type User, type Role } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { GlassPanel } from '../components/glass/GlassPanel'
import { GlassButton } from '../components/glass/GlassButton'
import { AdminLinks } from '../components/AdminLinks'

function UserRow({ user }: { user: User }) {
  const [roles, setRoles] = useState<Role[]>(user.roles)
  const [enabled, setEnabled] = useState(user.enabled)
  const query = useQueryClient()
  const mutation = useMutation({ mutationFn: () => updateUser(user.id, roles, enabled),
    onSuccess: async () => {
      if (useAuthStore.getState().user?.id === user.id) useAuthStore.getState().setUser(await getMe())
      await query.invalidateQueries({ queryKey: ['roles'] })
    },
  })
  return <tr className="border-t border-slate-500/10 align-top">
    <td className="px-3 py-5"><p className="font-medium">{user.display_name || user.username}</p><p className="mt-1 text-xs text-slate-500">{user.username}</p></td>
    <td className="px-3 py-5"><div className="flex flex-wrap gap-4">{(Object.keys(roleLabels) as Role[]).map(role => <label key={role} className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={roles.includes(role)} onChange={event => setRoles(event.target.checked ? [...roles, role] : roles.filter(value => value !== role))} />{roleLabels[role]}</label>)}</div></td>
    <td className="px-3 py-5"><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />启用</label></td>
    <td className="px-3 py-4"><GlassButton onClick={() => mutation.mutate()} disabled={mutation.isPending}>保存</GlassButton>
      {mutation.isError && <p role="alert" className="mt-2 text-xs text-red-800">{mutation.error.message}</p>}
      {mutation.isSuccess && <p role="status" className="mt-2 text-xs text-emerald-800">已保存</p>}</td>
  </tr>
}
export function RolesPage() {
  const query = useQuery({ queryKey: ['roles'], queryFn: listUsers, retry: false })
  return <GlassPanel><AdminLinks /><h1 className="text-2xl font-semibold">角色表</h1><p className="mb-7 mt-3 text-sm text-slate-500">维护已登录用户的工作台角色与启用状态。NAS 目录权限由群晖管理。</p>
    {query.isPending ? <p>正在加载用户…</p> : query.isError ? <p role="alert" className="text-red-800">{query.error.message}</p> :
      <table className="w-full text-left"><thead className="text-xs text-slate-500"><tr><th className="p-3">账号</th><th className="p-3">角色（可多选）</th><th className="p-3">状态</th><th className="p-3">操作</th></tr></thead>
        <tbody>{query.data.map(user => <UserRow key={`${user.id}:${user.roles.join(',')}:${user.enabled}`} user={user} />)}</tbody></table>}
  </GlassPanel>
}
