export type VisualMode = 'full' | 'lite'
export const MODE_KEY = 'nas-workbench-visual-mode'

export function initialMode(saved: string | null, userAgent: string): VisualMode {
  if (saved === 'full' || saved === 'lite') return saved
  return /Firefox|Safari/.test(userAgent) && !/Chrome|Chromium|Edg/.test(userAgent) ? 'lite' : 'full'
}
