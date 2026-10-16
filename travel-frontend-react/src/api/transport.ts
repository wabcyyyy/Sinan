import { isDemoMode } from '../demoMode'

/** 同一 API 消费面：演示仅返回本地数据；正式模式原样使用浏览器 fetch。 */
export async function apiFetch(url: string, init: RequestInit): Promise<Response> {
  if (isDemoMode()) return (await import('../react-app/demo/transport')).demoFetch(url, init)
  return globalThis.fetch(url, init)
}
