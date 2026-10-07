import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { assertGoldenDelivery } from './delivery-gate.mjs'

function deliveredTrip() {
  return {
    status: 2, days: 2, stayNights: 1, qualityStatus: 'READY_WITH_WARNINGS', totalAmount: 640, budget: 1000,
    dayList: [
      { dayNo: 1, generationStatus: 'SUCCEEDED', items: [{ itemType: 'hotel', poiName: '基准酒店' }] },
      { dayNo: 2, generationStatus: 'SUCCEEDED', items: [{ itemType: 'food', poiName: '餐厅' }] },
    ],
  }
}

test('完整交付且事实需核实仍可通过', () => assertGoldenDelivery(deliveredTrip()))

test('行程 189：页面完成但第二天 PENDING 为空必须失败', () => {
  const trip = deliveredTrip()
  trip.qualityStatus = 'DRAFT'
  trip.dayList[1] = { dayNo: 2, generationStatus: 'PENDING', items: [] }
  assert.throws(() => assertGoldenDelivery(trip), /交付质量未就绪/)
  trip.qualityStatus = 'READY_WITH_WARNINGS'
  assert.throws(() => assertGoldenDelivery(trip), /第 2 天未成功交付/)
})

test('住宿缺失、重复、退房日入住均失败', () => {
  for (const mutate of [
    trip => { trip.dayList[0].items = [{ itemType: 'food', poiName: '餐厅' }] },
    trip => { trip.dayList[0].items.push({ itemType: 'hotel', poiName: '额外午休酒店' }) },
    trip => { trip.dayList[1].items.push({ itemType: 'hotel', poiName: '基准酒店' }) },
  ]) {
    const trip = deliveredTrip()
    mutate(trip)
    assert.throws(() => assertGoldenDelivery(trip), /住宿晚次错误/)
  }
})

test('空日、缺日、重复日均失败', () => {
  for (const mutate of [
    trip => { trip.dayList[1].items = [] },
    trip => { trip.dayList.pop() },
    trip => { trip.dayList[1].dayNo = 1 },
  ]) {
    const trip = deliveredTrip()
    mutate(trip)
    assert.throws(() => assertGoldenDelivery(trip), /为空|行程日缺失或重复/)
  }
})

test('预算超支必须已有后端告警，零金额不算重算完成', () => {
  const trip = deliveredTrip()
  trip.totalAmount = 0
  assert.throws(() => assertGoldenDelivery(trip), /预算尚未重算/)
  trip.totalAmount = 640
  trip.budget = 500
  assert.throws(() => assertGoldenDelivery(trip), /预算超支缺少告警/)
  trip.qualityReport = { warnings: [{ code: 'BUDGET_EXCEEDED' }] }
  assertGoldenDelivery(trip)
})
