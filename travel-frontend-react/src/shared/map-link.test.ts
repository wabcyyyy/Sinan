import { describe, expect, it } from 'vitest'
import { safeAppLink, safeMapLink } from './map-link'

describe('外部地图深链白名单', () => {
  it('放行后端 map_link 的两个出口', () => {
    expect(safeMapLink('https://uri.amap.com/marker?position=120.1,30.2&name=x')).toContain('uri.amap.com')
    expect(safeMapLink('https://www.google.com/maps/search/?api=1&query=Tokyo')).toContain('google.com/maps')
  })
  it('拒绝非白名单主机、非 https 与垃圾输入', () => {
    expect(safeMapLink('https://evil.example.com/maps?x=1')).toBe('')
    expect(safeMapLink('http://uri.amap.com/marker?position=1,2')).toBe('')
    expect(safeMapLink('javascript:alert(1)')).toBe('')
    expect(safeMapLink('not a url')).toBe('')
    expect(safeMapLink(null)).toBe('')
  })
  it('谷歌主机只放行 /maps 路径', () => {
    expect(safeMapLink('https://www.google.com/search?q=hotel')).toBe('')
    expect(safeMapLink('https://google.com/maps/place/x')).toContain('google.com/maps')
  })
})

describe('站内链接同源校验（P1-6：shareUrl / downloadUrl 不裸进 href）', () => {
  const ORIGIN = 'https://app.example.com'
  it('放行后端实际下发的单斜杠相对路径', () => {
    expect(safeAppLink('/s/abc123', ORIGIN)).toBe('/s/abc123')
    expect(safeAppLink('/api/export/download/7', ORIGIN)).toBe('/api/export/download/7')
  })
  it('拒绝协议相对与异源绝对 URL', () => {
    expect(safeAppLink('//evil.example.com/x', ORIGIN)).toBeNull()
    expect(safeAppLink('https://evil.example.com/s/t', ORIGIN)).toBeNull()
    expect(safeAppLink('http://app.example.com/s/t', ORIGIN)).toBeNull()
  })
  it('放行与当前源一致的绝对 URL', () => {
    expect(safeAppLink('https://app.example.com/s/tok', ORIGIN)).toContain('/s/tok')
  })
  it('拒绝危险协议与垃圾输入', () => {
    expect(safeAppLink('javascript:alert(1)', ORIGIN)).toBeNull()
    expect(safeAppLink('data:text/html,x', ORIGIN)).toBeNull()
    expect(safeAppLink('not a url', ORIGIN)).toBeNull()
    expect(safeAppLink(null, ORIGIN)).toBeNull()
  })
})