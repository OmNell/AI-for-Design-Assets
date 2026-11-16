export interface LocalEntry { rel_path: string; file?: File; is_dir: boolean }
export interface DirectorySelection { folder_name: string; entries: LocalEntry[]; preservesEmptyDirectories: boolean }

export async function readDrop(items: DataTransferItemList): Promise<DirectorySelection> {
  const roots = Array.from(items).filter(item => item.kind === 'file').map(item => item.webkitGetAsEntry())
  if (roots.length !== 1 || !roots[0]?.isDirectory) throw new Error('一次只能拖入一个项目文件夹')
  const root = roots[0]
  const entries: LocalEntry[] = []
  async function walk(entry: FileSystemEntry, path: string) {
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject))
      entries.push({ rel_path: path, file, is_dir: false })
    } else {
      if (path) entries.push({ rel_path: path, is_dir: true })
      const reader = (entry as FileSystemDirectoryEntry).createReader()
      // Chromium 每批最多返回约 100 项，必须读到空批次。
      for (;;) {
        const children = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
        if (!children.length) break
        for (const child of children) await walk(child, path ? `${path}/${child.name}` : child.name)
      }
    }
  }
  await walk(root, '')
  return { folder_name: root.name, entries, preservesEmptyDirectories: true }
}

interface PickerFile { kind: 'file'; name: string; getFile(): Promise<File> }
interface PickerDirectory { kind: 'directory'; name: string; values(): AsyncIterable<PickerDirectory | PickerFile> }
export async function readPicker(): Promise<DirectorySelection | null> {
  const picker = (window as Window & { showDirectoryPicker?: () => Promise<PickerDirectory> }).showDirectoryPicker
  if (!picker) return null
  const root = await picker()
  const entries: LocalEntry[] = []
  async function walk(directory: PickerDirectory, prefix: string) {
    for await (const child of directory.values()) {
      const path = prefix ? `${prefix}/${child.name}` : child.name
      if (child.kind === 'directory') { entries.push({ rel_path: path, is_dir: true }); await walk(child, path) }
      else entries.push({ rel_path: path, file: await child.getFile(), is_dir: false })
    }
  }
  await walk(root, '')
  return { folder_name: root.name, entries, preservesEmptyDirectories: true }
}

export function readInput(files: FileList | File[]): DirectorySelection {
  const values = Array.from(files)
  const roots = new Set(values.map(file => file.webkitRelativePath.split('/')[0]))
  if (roots.size !== 1 || !values.length || values.some(file => !file.webkitRelativePath.includes('/'))) throw new Error('请选择一个项目文件夹；空目录请使用拖入方式')
  const entries: LocalEntry[] = []
  const directories = new Set<string>()
  for (const file of values) {
    const path = file.webkitRelativePath.split('/').slice(1).join('/')
    const parts = path.split('/')
    for (let index = 1; index < parts.length; index++) directories.add(parts.slice(0, index).join('/'))
    entries.push({ rel_path: path, file, is_dir: false })
  }
  return { folder_name: [...roots][0], entries: [...[...directories].map(rel_path => ({ rel_path, is_dir: true })), ...entries], preservesEmptyDirectories: false }
}
