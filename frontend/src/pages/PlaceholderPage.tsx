import { Panel } from '../components/ui'
export function PlaceholderPage({ title }: { title: string }) {
  return <Panel><h1 className="text-xl font-semibold">{title}</h1></Panel>
}
