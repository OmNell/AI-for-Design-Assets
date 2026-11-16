import { GlassButton } from './GlassButton'
import { useGlassMode } from './GlassProvider'

export function ModeSwitch() {
  const { mode, toggle } = useGlassMode()
  return <GlassButton onClick={toggle} aria-label="切换视觉模式" aria-pressed={mode === 'full'}>
    视觉模式 · {mode === 'full' ? '完整' : '轻量'}
  </GlassButton>
}
