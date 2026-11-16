import { createContext, useContext, useState, type PropsWithChildren } from 'react'
import { initialMode, MODE_KEY, type VisualMode } from './visualMode'

interface GlassContextValue { mode: VisualMode; toggle: () => void }
const GlassContext = createContext<GlassContextValue>({ mode: 'lite', toggle: () => undefined })
export const useGlass = (): GlassContextValue => useContext(GlassContext)

export function GlassProvider({ children }: PropsWithChildren) {
  const [mode, setMode] = useState<VisualMode>(() => {
    let saved: string | null = null
    try { saved = localStorage.getItem(MODE_KEY) } catch { /* 浏览器禁用存储时使用默认档。 */ }
    return initialMode(saved, navigator.userAgent)
  })
  const toggle = () => setMode(current => {
    const next = current === 'full' ? 'lite' : 'full'
    try { localStorage.setItem(MODE_KEY, next) } catch { /* 存储失败不影响切换。 */ }
    return next
  })
  return <GlassContext.Provider value={{ mode, toggle }}>{children}</GlassContext.Provider>
}
