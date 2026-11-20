const paths = {
  brand: 'M4 10h16v10H4z M3 4h18v5H3z M9 14h6',
  home: 'm3 10 9-7 9 7v10h-6v-6H9v6H3z',
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  upload: 'M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4',
  inbox: 'M4 5h16l2 10v4H2v-4zm-2 10h6l2 2h4l2-2h6',
  check: 'm5 12 4 4L19 6',
  search: 'M15.5 15.5 21 21m-3-11a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0-5v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4',
  logout: 'M10 4H4v16h6m4-4 4-4-4-4m-5 4h10',
  right: 'm9 6 6 6-6 6',
  down: 'm6 9 6 6 6-6',
  close: 'm6 6 12 12M18 6 6 18',
  panel: 'M3 5h18v14H3zm11 0v14',
  file: 'M6 3h8l4 4v14H6zm8 0v5h4M9 12h6m-6 4h6',
  image: 'M3 4h18v16H3zm0 12 5-5 4 4 3-3 6 6M16 8h.01',
  list: 'M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01',
  grid: 'M4 4h6v6H4zm10 0h6v6h-6zM4 14h6v6H4zm10 0h6v6h-6z',
  progress: 'M20 12a8 8 0 1 1-8-8',
  arrow: 'M4 12h15m-6-6 6 6-6 6',
  clock: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zm-9-5v5l3 2',
  users: 'M16 20v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2m13-6a4 4 0 0 1 3 4v2M14 8a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  activity: 'M3 12h4l3-7 4 14 3-7h4',
  download: 'M12 4v12m-5-5 5 5 5-5M4 17v3h16v-3',
}
export type IconName = keyof typeof paths
export function Icon({ name, className = '' }: { name: IconName; className?: string }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round"
    strokeLinejoin="round" className={`size-4 shrink-0 ${className}`}><path d={paths[name]} /></svg>
}
