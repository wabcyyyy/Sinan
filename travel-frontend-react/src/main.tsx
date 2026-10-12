import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './react-app/App'
import { ErrorBoundary } from './react-app/shared/ErrorBoundary'
import { installErrorProbe } from './react-app/shared/errorProbe'
import { MotionRoot } from './react-app/shared/motion'
import './react-app/styles.css'

// 探针先于渲染装好：首帧渲染期的异常也要有通道
installErrorProbe()

createRoot(document.getElementById('app')!).render(
  <StrictMode>
    <ErrorBoundary>
      <MotionRoot>
        <App />
      </MotionRoot>
    </ErrorBoundary>
  </StrictMode>,
)
