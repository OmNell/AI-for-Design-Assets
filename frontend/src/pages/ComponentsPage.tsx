import { useState } from 'react'
import { Badge, Button, Card, Checkbox, Dialog, EmptyState, Input, Panel, Select, Table, Textarea } from '../components/ui'

export function ComponentsPage() {
  const [open, setOpen] = useState(false)
  return <div className="space-y-4"><h1 className="text-2xl font-semibold">组件样例</h1>
    <Panel><div className="flex flex-wrap items-center gap-2">
      <Button onClick={() => setOpen(true)}>打开弹窗</Button><Button disabled>禁用按钮</Button>
      <Button loading>加载中</Button><Badge tone="success">已完成</Badge><Badge tone="warning">待处理</Badge><Badge tone="danger">失败</Badge>
    </div></Panel>
    <div className="grid gap-4 lg:grid-cols-2">
      <Card><h2 className="mb-3 font-semibold">表单控件</h2><div className="space-y-3">
        <label className="block">项目名称 <Input placeholder="请输入项目名称" /></label>
        <label className="block">中标状态 <Select><option>已中标</option><option>未中标</option></Select></label>
        <label className="block">备注 <Textarea rows={2} /></label>
        <label className="flex items-center gap-2"><Checkbox />确认资料完整</label>
      </div></Card>
      <Card><h2 className="mb-3 font-semibold">表格与空状态</h2><Table label="组件样例表格"><thead><tr><th>项目</th><th>状态</th></tr></thead>
        <tbody><tr><td colSpan={2}><EmptyState title="暂无项目" /></td></tr></tbody></Table></Card>
    </div>
    <Dialog open={open} title="弹窗示例" onClose={() => setOpen(false)}><p className="text-slate-500">使用 Escape 或关闭按钮返回。</p></Dialog>
  </div>
}
