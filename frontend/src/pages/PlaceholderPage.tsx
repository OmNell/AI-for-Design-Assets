import { GlassPanel } from '../components/glass/GlassPanel'
export function PlaceholderPage({ title }: { title: string }) {
  return <GlassPanel><p className="text-sm text-emerald-800">{title}</p><h1 className="mt-5 text-3xl font-semibold">建设中</h1><p className="mt-5 text-slate-500">此页面将在后续工作包开放。</p></GlassPanel>
}
