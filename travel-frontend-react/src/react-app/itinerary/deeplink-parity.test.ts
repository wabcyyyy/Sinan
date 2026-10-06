import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { DayPlan } from '../../types/itinerary'
import { amapLink, buildPins, googleMapsLink } from './mapPins'

/**
 * 跨端深链 parity（Y4 延续，2026-10-04 恢复）：与后端
 * travel-agent-python/tests/test_deeplink_parity.py 读**同一份**
 * tests/golden/deeplink_cases.json，各断言各自实现的当前协议口径——
 * 任何一侧单方面改语义都会在golden case上显形。
 *
 * React 端口径（旧组件树的搜索/路线深链已随退役删除）：
 * - 只按 pin 出两条「核实」链接：高德 marker / 谷歌查询，构造后过
 *   shared/map-link 白名单（链接若被白名单拒绝会返回空串，这里即红）；
 * - 有效坐标才出链接：hasValidCoords 与后端 _parse_coords 同语义
 *   （有限值 + 范围 + 非 0/0 哨兵），无效视同无坐标（2026-10-04 拍板补齐，
 *   原 R2 差异消解）；
 * - 无/无效坐标点位不进 pin（buildPins 跳过），路线深链无实现。
 *
 * 断言粒度与后端一致：协议 host + path 前缀 + 关键查询参数存在；
 * 坐标数值换算等属实现细节，不整串相等。
 */

interface LinkExpectation {
  host: string
  path: string
  params?: string[]
}

interface ParityCase {
  name: string
  input: { name?: string; city?: string; latitude?: number | null; longitude?: number | null; stops?: unknown[] }
  frontend: { null?: boolean; pin_links?: LinkExpectation[] }
}

const CASES_PATH = '../travel-agent-python/tests/golden/deeplink_cases.json'
const cases = (JSON.parse(readFileSync(resolve(process.cwd(), CASES_PATH), 'utf8')) as { cases: ParityCase[] }).cases

function assertProtocol(url: string | null, link: LinkExpectation, note: string): void {
  const suffix = note ? `（${note}）` : ''
  expect(url, `应产出链接${suffix}`).toBeTruthy()
  const parts = new URL(url!)
  expect(parts.hostname, `${url}${suffix}`).toBe(link.host)
  expect(parts.pathname.startsWith(link.path), `${url}${suffix}`).toBe(true)
  for (const key of link.params ?? []) {
    expect(parts.searchParams.get(key), `missing ${key}: ${url}${suffix}`).toBeTruthy()
  }
}

describe('深链 parity：与后端共读 golden/deeplink_cases.json', () => {
  it('golden case 文件存在且非空（跨端共享 fixture 不能断链）', () => {
    expect(cases.length).toBeGreaterThan(0)
  })

  for (const testCase of cases) {
    const { frontend } = testCase

    if (frontend.null) {
      const isRoute = Array.isArray(testCase.input.stops)
      it(`${testCase.name}：前端不产出深链（${isRoute ? '路线深链已随 React 迁移退役' : '无坐标不进 pin'}）`, () => {
        expect(frontend.null).toBe(true)
        if (!isRoute) {
          // 无坐标 case：点位根本建不成 pin，自然无链接可出
          const days = [{
            dayId: 1,
            dayNo: 1,
            generationStatus: 'SUCCEEDED',
            items: [{
              itemType: 'attraction',
              poiName: testCase.input.name ?? '',
              latitude: testCase.input.latitude ?? null,
              longitude: testCase.input.longitude ?? null,
              valueKind: 'observed',
              startTime: null,
              key: 0,
            }],
          }] as unknown as DayPlan[]
          expect(buildPins(days)).toHaveLength(0)
        }
      })
      continue
    }

    const pin = {
      latitude: testCase.input.latitude!,
      longitude: testCase.input.longitude!,
      poiName: testCase.input.name ?? '',
    }
    const amapExpect = frontend.pin_links?.find((link) => link.host === 'uri.amap.com')
    const googleExpect = frontend.pin_links?.find((link) => link.host.includes('google.com'))
    it(`${testCase.name}：pin 双核实链接符合 golden 口径（高德 marker + 谷歌查询，过白名单）`, () => {
      expect(amapExpect, 'fixture 必须登记高德链期望').toBeTruthy()
      expect(googleExpect, 'fixture 必须登记谷歌链期望').toBeTruthy()
      assertProtocol(amapLink(pin), amapExpect!, 'amapLink')
      assertProtocol(googleMapsLink(pin), googleExpect!, 'googleMapsLink')
    })
  }
})
