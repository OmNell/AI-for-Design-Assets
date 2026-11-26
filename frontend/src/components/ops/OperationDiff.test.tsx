import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router'
import { expect, it } from 'vitest'
import { OperationDiff } from './OperationDiff'
import type { OperationManifest } from '../../api/operations'

const before: OperationManifest = {
  project: { id: 1, project_code: 'P020', project_name: '样本项目', company_name: '样本企业', won: true, status: 'active' },
  products: [], files: [],
}
const render = (child: React.ReactNode) => renderToStaticMarkup(<MemoryRouter>{child}</MemoryRouter>)

it('审核差异同时展示原标签和显式确认的空标签', () => {
  const html = render(<OperationDiff operation={{ kind: 'retag', before, params: {}, after: {
    tags: { 'P020-001:festival': {
      before: { value: '中秋', confirmation_status: 'confirmed_value' },
      after: { value: '', confirmation_status: 'confirmed_unknown' },
    } },
  } }} />)
  expect(html).toContain('节日场景')
  expect(html).toContain('中秋')
  expect(html).toContain('（空）')
  expect(html).toContain('已确认无法判断')
})

it('物理删除清单分别标出同一文件的当前副本和历史副本', () => {
  const target = { file_id: 1, size: 1024, fingerprint: 'a'.repeat(64) }
  const html = render(<OperationDiff operation={{ kind: 'purge', before, params: {}, purge: {
    project_code: 'P020', confirm_label: 'P020', eligible: true, earliest_allowed: '2026-11-01',
    files: [{ ...target, path: '现行/杯.jpg', historical: false }, { ...target, path: '旧版/杯.jpg', historical: true }],
    products: [], history: [{ file_id: 1 }], relations: [],
  } }} />)
  expect(html).toContain('现行/杯.jpg')
  expect(html).toContain('旧版/杯.jpg')
  expect(html.match(/历史版本/g)).toHaveLength(1)
  expect(html).toContain('2 个文件')
})
