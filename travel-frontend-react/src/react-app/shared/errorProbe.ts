import { reportClientError } from '../../api/sinan'

const INSTALL_FLAG = '__sinanErrorProbeInstalled'

type ProbeWindow = Window & { [INSTALL_FLAG]?: boolean }

/** 全局错误探针：window 级异常与未处理 Promise 拒绝统一进上报通道（P0 可观测性）。
 * 只装一次（模块热替换/重复调用幂等）；资源加载失败（非 ErrorEvent）刻意不上报——
 * 无 message 可归因，噪声大于信号。 */
export function installErrorProbe(): void {
  const probeWindow = window as ProbeWindow
  if (probeWindow[INSTALL_FLAG]) return
  probeWindow[INSTALL_FLAG] = true

  const context = () => ({
    path: window.location.pathname,
    userAgent: navigator.userAgent,
    ts: new Date().toISOString(),
  })

  window.addEventListener('error', (event) => {
    if (!(event instanceof ErrorEvent)) return
    reportClientError({
      message: event.message || 'unknown error',
      stack: event.error instanceof Error ? event.error.stack || null : null,
      source: 'window',
      ...context(),
    })
  })

  window.addEventListener('unhandledrejection', (event) => {
    const reason: unknown = event.reason
    reportClientError({
      message: reason instanceof Error ? reason.message : String(reason ?? 'unhandled rejection'),
      stack: reason instanceof Error ? reason.stack || null : null,
      source: 'unhandledrejection',
      ...context(),
    })
  })
}
