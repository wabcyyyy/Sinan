import { describe, expect, it } from 'vitest'
import { detailGeneratingCopy } from './detailBanner'

describe('detailGeneratingCopy（详情页 core_ready 刷新投影）', () => {
  it('coreReady=true：如实说明主行程已可查看、备选仍在完善（与 TripPanel 轻提示同措辞）', () => {
    const copy = detailGeneratingCopy(true)
    expect(copy).toContain('主行程已可查看')
    expect(copy).toContain('备选仍在完善')
    expect(copy).not.toContain('正在逐日编排')
  })

  it('coreReady 缺省/false（存量行、GENERATING）：保持原「正在逐日编排」文案', () => {
    expect(detailGeneratingCopy()).toContain('正在逐日编排')
    expect(detailGeneratingCopy(false)).toContain('正在逐日编排')
    expect(detailGeneratingCopy(undefined)).not.toContain('主行程已可查看')
  })
})
