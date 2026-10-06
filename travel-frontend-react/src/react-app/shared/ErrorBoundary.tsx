import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { reportClientError } from '../../api/sinan'
import { ErrorBlock } from './States'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  message: string | null
}

/** 根级错误边界：渲染抛错不再白屏，复用 States 的错误态视觉（零新增 CSS）。
 * 事件处理器里的异常不走边界（React 语义），由 errorProbe 的 window 级监听兜住；
 * 两者共用 reportClientError 的会话去重，同一次崩溃不会双报成两条日志。 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { message: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { message: error.message || '渲染组件时出错' }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    reportClientError({
      message: error.message,
      stack: error.stack || null,
      source: 'errorboundary',
      path: window.location.pathname,
      userAgent: navigator.userAgent,
      ts: new Date().toISOString(),
    })
    // componentStack 含组件私有树且动辄超长，不进上报；开发期留在控制台
    console.error(info.componentStack)
  }

  render(): ReactNode {
    if (this.state.message !== null) {
      return <ErrorBlock message={`页面出了点问题：${this.state.message}`} onRetry={() => window.location.reload()} />
    }
    return this.props.children
  }
}
