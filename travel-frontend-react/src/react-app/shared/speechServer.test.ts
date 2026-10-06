import { act, createElement, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ReactApiError, interpretSpeechIntent } from '../../api/sinan'
import { serverSpeechSupported, speechSupported } from './speechText'
import { ChatComposer } from './ChatComposer'

/** 服务端 ASR 链路（2026-10-04）：录音 → POST /speech-intent（LLM_ROLE_STT，默认 MiMo）。
 *
 * 为什么单开一组用例：Web Speech 依赖浏览器厂商在线服务、国内网络经常不可用，所以
 * 「能录音就走服务端」是**首选**通道，Web Speech 退化为兜底。这里用假 MediaRecorder
 * 驱动真 createRoot + act，钉住接线：录音停止后确实调了接口、结果确实写进输入框、
 * 失败有可见反馈。happy-dom 没有 MediaRecorder，能力用例自己往 window 挂。 */

vi.mock('../../api/sinan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/sinan')>()
  return { ...actual, interpretSpeechIntent: vi.fn() }
})

const mockedInterpret = vi.mocked(interpretSpeechIntent)

const baseProps = {
  value: '',
  onChange: () => {},
  onSend: () => {},
  placeholder: '例如：第二天加个博物馆',
  ariaLabel: '对行程说话',
  maxLength: 2000,
  className: 'chat-composer',
}

class FakeMediaRecorder {
  static instances: FakeMediaRecorder[] = []
  static mimeType = 'audio/webm'
  state: 'inactive' | 'recording' = 'inactive'
  mimeType = 'audio/webm'
  stream: { getTracks: () => Array<{ stop: () => void }> }
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null

  constructor(stream: { getTracks: () => Array<{ stop: () => void }> }) {
    this.stream = stream
    FakeMediaRecorder.instances.push(this)
  }

  start() {
    this.state = 'recording'
  }

  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['fake-audio'], { type: this.mimeType }) })
    this.onstop?.()
  }
}

function installRecorder() {
  const tracksStopped: number[] = []
  const stream = { getTracks: () => [{ stop: () => tracksStopped.push(1) }] }
  ;(window as unknown as Record<string, unknown>).MediaRecorder = FakeMediaRecorder
  Object.defineProperty(window.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn(async () => stream) },
  })
  return tracksStopped
}

async function mountComposer() {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  const Harness = () => {
    const [draft, setDraft] = useState('想去成都')
    return createElement(ChatComposer, { ...baseProps, value: draft, onChange: setDraft })
  }
  await act(async () => {
    root.render(createElement(Harness))
  })
  return {
    input: () => container.querySelector('input[aria-label="对行程说话"]') as HTMLInputElement,
    micButton: () =>
      container.querySelector('button[aria-label="语音输入"], button[aria-label="停止语音输入"]') as HTMLButtonElement,
    note: () => container.querySelector('.composer-note')?.textContent ?? '',
  }
}

describe('serverSpeechSupported（录音能力探测）', () => {
  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).MediaRecorder
  })

  it('happy-dom 默认既不能录音也没有 Web Speech → 麦克风不渲染', () => {
    expect(serverSpeechSupported()).toBe(false)
    expect(speechSupported()).toBe(false)
  })

  it('有 MediaRecorder + 麦克风 API 即为可用（无需 Web Speech）', () => {
    installRecorder()
    expect(serverSpeechSupported()).toBe(true)
    expect(speechSupported()).toBe(false) // 关键：Web Speech 缺失也不影响服务端通道
  })
})

describe('ChatComposer 语音交互（服务端 ASR）', () => {
  const originalMediaDevices = Object.getOwnPropertyDescriptor(window.navigator, 'mediaDevices')

  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    installRecorder()
    mockedInterpret.mockReset()
  })

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).MediaRecorder
    if (originalMediaDevices) Object.defineProperty(window.navigator, 'mediaDevices', originalMediaDevices)
    else delete (window.navigator as unknown as Record<string, unknown>).mediaDevices
    FakeMediaRecorder.instances = []
    document.querySelectorAll('div').forEach((node) => node.remove())
  })

  it('录音 → 停止 → 上传 → 转写追加到底稿后（结果交用户编辑，不自动发送）', async () => {
    mockedInterpret.mockResolvedValue({ text: '玩四天', suggestedMessage: '玩四天' })
    const ui = await mountComposer()

    await act(async () => {
      ui.micButton().click()
    })
    const recorder = FakeMediaRecorder.instances.at(-1) as FakeMediaRecorder
    expect(recorder.state).toBe('recording')
    expect(ui.micButton().getAttribute('aria-label')).toBe('停止语音输入')

    await act(async () => {
      ui.micButton().click()
    })
    expect(mockedInterpret).toHaveBeenCalledTimes(1)
    expect(ui.input().value).toBe('想去成都玩四天')
    expect(ui.micButton().getAttribute('aria-label')).toBe('语音输入')
  })

  it('接口失败时把后端原因显出来（配置错要能定位，不能只报"失败"）', async () => {
    mockedInterpret.mockRejectedValue(new ReactApiError('当前语音通道不支持语音识别：请配 LLM_ROLE_STT', 502))
    const ui = await mountComposer()

    await act(async () => {
      ui.micButton().click()
    })
    await act(async () => {
      ui.micButton().click()
    })
    expect(ui.note()).toContain('LLM_ROLE_STT')
  })

  it('录音结束停掉音轨（不占着麦克风）', async () => {
    mockedInterpret.mockResolvedValue({ text: '好', suggestedMessage: '好' })
    const tracksStopped = installRecorder()
    const ui = await mountComposer()
    await act(async () => {
      ui.micButton().click()
    })
    await act(async () => {
      ui.micButton().click()
    })
    expect(tracksStopped.length).toBe(1)
  })
})
