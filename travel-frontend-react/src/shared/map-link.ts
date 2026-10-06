/**
 * 外部地图深链的安全出口（框架无关纯函数）。
 *
 * 后端 `app/agent/data/map_link.py` 只产出两种深链：国内高德 URI、海外谷歌 Maps。
 * 契约字段（如酒店候选卡 `searchLink`）会原样进 `href`，因此渲染前必须过白名单：
 * 载荷里出现非白名单域名（改后端模板、旧版本残留、数据被改写）时**不渲染链接**，
 * 而不是把任意 URL 交给用户点。
 *
 * 取舍（审查 P1-5 点名的"前端常量或后端下发"）：用**前端常量**。理由——
 * 白名单是渲染侧的防御，不是业务数据；由后端下发等于给同一个静态事实再加一次
 * 线上往返与一个契约字段，且"白名单"本身要能被篡改的那份数据改变，就失去意义。
 * 与后端 `map_link.py` 的同源关系由两侧测试读同一份 fixture 钉住（后端
 * tests/test_deeplink_parity.py、前端 react-app/itinerary/deeplink-parity.test.ts，
 * 共享 tests/golden/deeplink_cases.json）。
 */

/** 允许的深链主机（与后端 map_link 出口一一对应）。 */
export const MAP_LINK_HOSTS = ['uri.amap.com', 'www.google.com', 'google.com'] as const

/** 谷歌域下只有 /maps/ 路径是地图深链（同主机还有账号/广告等无关页面）。 */
const GOOGLE_PATH_PREFIX = '/maps'

export function safeMapLink(url: string | null | undefined): string {
  const text = (url || '').trim()
  if (!text) return ''
  let parsed: URL
  try {
    parsed = new URL(text)
  } catch {
    return ''
  }
  if (parsed.protocol !== 'https:') return ''
  const host = parsed.hostname.toLowerCase()
  if (!(MAP_LINK_HOSTS as readonly string[]).includes(host)) return ''
  if (host.endsWith('google.com') && !parsed.pathname.startsWith(GOOGLE_PATH_PREFIX)) return ''
  return parsed.toString()
}

/**
 * 站内链接的安全出口（分享 `shareUrl=/s/{token}`、导出 `downloadUrl=/api/export/download/{id}`）。
 *
 * 后端 share_service / export_service 实际只下发**单斜杠站内相对路径**，但契约字段会
 * 原样进 `href`：一旦被改写成 `javascript:`、`//evil` 或异源绝对 URL，就是任意跳转。
 * 与 safeMapLink 同一条纪律——渲染前过校验，不过（null）就退化为纯文本而非可点链接。
 *
 * 放行口径（其余一律 null）：
 * ① 以单个 `/` 开头的站内相对路径（`//` 开头是协议相对 URL，会跳到任意源，拒）；
 * ② 绝对 URL 中 origin 与当前源一致者（`origin` 参数供纯 Node 单测注入，
 *    免依赖 window.location）。
 */
export function safeAppLink(raw: string | null | undefined, origin?: string): string | null {
  const text = (raw || '').trim()
  if (!text) return null
  if (text.startsWith('/')) {
    return text.startsWith('//') ? null : text
  }
  let parsed: URL
  try {
    parsed = new URL(text)
  } catch {
    return null
  }
  const base = origin ?? (typeof window === 'undefined' ? '' : window.location.origin)
  if (!base || parsed.origin !== base) return null
  return parsed.toString()
}