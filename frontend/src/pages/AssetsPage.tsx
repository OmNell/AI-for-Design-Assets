import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { Button } from '../components/ui'
import { NasBrowserPage } from './NasBrowserPage'
import { ProjectsPage } from './ProjectsPage'

const preference = 'workbench.assets.mode'

export function AssetsPage() {
  const [saved, setSaved] = useState(() => localStorage.getItem(preference) === 'catalog' ? 'catalog' : 'nas')
  const [params, setParams] = useSearchParams()
  const classified = ['won', 'industry', 'company'].some(field => params.has(field))
  const mode = classified ? 'catalog' : saved
  const switchTo = (value: 'catalog' | 'nas') => {
    setSaved(value)
    localStorage.setItem(preference, value)
    if (value === 'nas' && classified) {
      const next = new URLSearchParams(params)
      for (const key of ['won', 'industry', 'company']) next.delete(key)
      setParams(next)
    }
  }
  return <div className="min-w-0 space-y-4">
    <h1 className="text-2xl font-semibold tracking-tight">全部资产</h1>
    <div className="flex flex-wrap gap-2" role="group" aria-label="资产来源">
      <Button primary={mode === 'nas'} aria-pressed={mode === 'nas'} onClick={() => switchTo('nas')}>NAS 文件浏览</Button>
      <Button primary={mode === 'catalog'} aria-pressed={mode === 'catalog'} onClick={() => switchTo('catalog')}>已入库资产</Button>
    </div>
    {mode === 'nas' ? <NasBrowserPage /> : <ProjectsPage embedded />}
  </div>
}
