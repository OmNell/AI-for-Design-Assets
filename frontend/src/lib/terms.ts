// 与后端 split_terms 相同的分隔规则；只用于界面展示，保存的仍是原始整段文字。
const SEPARATORS = /[、，,；;/\s]+/

export function splitTerms(value: string): string[] {
  return [...new Set(value.trim().split(SEPARATORS).filter(Boolean))]
}
export function removeTerm(value: string, term: string): string {
  return splitTerms(value).filter(item => item !== term).join('、')
}
/** 输入提示只针对正在输入的最后一段。 */
export function lastSegment(value: string): string {
  return value.split(/[、，,；;/]/).at(-1)!.trim()
}
export function replaceLastSegment(value: string, term: string): string {
  const index = Math.max(...['、', '，', ',', '；', ';', '/'].map(mark => value.lastIndexOf(mark)))
  return index < 0 ? term : value.slice(0, index + 1) + term
}
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = size / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++ }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`
}
