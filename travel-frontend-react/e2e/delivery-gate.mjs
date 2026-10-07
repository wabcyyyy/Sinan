import { strict as assert } from 'node:assert'

// 黄金路径的输入固定为 2 天 1 晚。页面上屏与地图可见不能证明逐日交付完整。
export function assertGoldenDelivery(detail, { days = 2, stayNights = 1 } = {}) {
  assert.equal(detail?.status, 2, '行程尚未交付')
  assert.equal(detail.days, days, '行程天数与输入不符')
  assert.equal(detail.stayNights, stayNights, '住宿晚数与输入不符')
  assert.ok(['READY', 'READY_WITH_WARNINGS'].includes(detail.qualityStatus), `交付质量未就绪：${detail.qualityStatus}`)
  const dayList = [...(detail.dayList || [])].sort((a, b) => a.dayNo - b.dayNo)
  assert.deepEqual(dayList.map(day => day.dayNo), Array.from({ length: days }, (_, i) => i + 1), '行程日缺失或重复')
  const hotelNames = []
  for (const day of dayList) {
    assert.equal(day.generationStatus, 'SUCCEEDED', `第 ${day.dayNo} 天未成功交付`)
    assert.ok(day.items?.length, `第 ${day.dayNo} 天为空`)
    const hotels = day.items.filter(item => item.itemType === 'hotel')
    assert.equal(hotels.length, day.dayNo <= stayNights ? 1 : 0, `第 ${day.dayNo} 天住宿晚次错误`)
    hotelNames.push(...hotels.map(item => item.poiName))
  }
  assert.ok(hotelNames.every(name => name && name === hotelNames[0]), '生成行程擅自换店')
  assert.ok(detail.totalAmount > 0, '最终预算尚未重算')
  if (detail.budget > 0 && detail.totalAmount > detail.budget) {
    const issues = [...(detail.qualityReport?.warnings || []), ...(detail.qualityReport?.blockingIssues || [])]
    assert.ok(issues.some(issue => issue.code === 'BUDGET_EXCEEDED'), '预算超支缺少告警')
  }
}
