import { act } from 'react'
import { createElement, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ReactApiError } from '../../api/sinan'
import { ChatComposer, composeDraft, imageFeedbackText } from './ChatComposer'

/** 对话输入共用件：静态渲染断言（不跑 effect、不依赖网络），与 homeIntake.test.ts 同纪律。
 * happy-dom 默认无 SpeechRecognition；「支持语音」用例直接往 window 挂假构造器。 */

const baseProps = {
  value: '',
  onChange: () => {},
  onSend: () => {},
  placeholder: '例如：第二天加个博物馆',
  ariaLabel: '对行程说话',
  maxLength: 2000,
  className: 'chat-composer',
}

const render = (props: Partial<Parameters<typeof ChatComposer>[0]> = {}) =>
  renderToStaticMarkup(createElement(ChatComposer, { ...baseProps, ...props }))

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).SpeechRecognition
})

describe('ChatComposer 静态渲染', () => {
  it('浏览器无 Web Speech：麦克风整个不渲染；图片按钮常驻，accept 只收三种图', () => {
    const html = render({ onImage: () => {} })
    expect(html).not.toContain('语音输入')
    expect(html).toContain('aria-label="识别图片"')
    expect(html).toContain('accept="image/jpeg,image/png,image/webp"')
    expect(html).toContain('发送')
  })
  it('带 Web Speech 实现时渲染麦克风按钮（默认未按下）', () => {
    ;(window as unknown as Record<string, unknown>).SpeechRecognition = class {}
    const html = render({ onImage: () => {} })
    expect(html).toContain('aria-label="语音输入"')
    expect(html).toContain('aria-pressed="false"')
  })
  it('不传 onImage 时图片按钮不渲染', () => {
    const html = render()
    expect(html).not.toContain('识别图片')
  })
  it('disabled 态：输入框与全部按钮一起禁用', () => {
    ;(window as unknown as Record<string, unknown>).SpeechRecognition = class {}
    const html = render({ value: '加个博物馆', disabled: true, onImage: () => {} })
    // input + 麦克风 + 图片 + 发送 四处全带 disabled
    expect((html.match(/disabled=""/g) || []).length).toBe(4)
  })
  it('空草稿时只有发送按钮禁用（其余可交互）', () => {
    ;(window as unknown as Record<string, unknown>).SpeechRecognition = class {}
    const html = render({ onImage: () => {} })
    expect((html.match(/disabled=""/g) || []).length).toBe(1)
    expect(html).toContain('发送')
  })
  it('识别图片中：图片按钮出 loading 态并禁用，发送不受影响', () => {
    const html = render({ value: '加个博物馆', onImage: () => {}, imageBusy: true })
    expect(html).toContain('正在识别图片')
    expect(html).toContain('composer-image is-busy')
    expect((html.match(/disabled=""/g) || []).length).toBe(1)
  })
  it('传入 placeholderList 时优先使用列表首项作为占位符', () => {
    const html = render({ placeholder: '默认占位符', placeholderList: ['建议占位符一', '建议占位符二'] })
    expect(html).toContain('placeholder="建议占位符一"')
  })
})

describe('composeDraft（图片建议消息回填）', () => {
  it('空底稿直接采用建议', () => {
    expect(composeDraft('', '想去图片里的地方', 2000)).toBe('想去图片里的地方')
  })
  it('非空底稿以空格相接，交用户编辑后照常发送', () => {
    expect(composeDraft('第二天加个博物馆', '顺便推荐附近的餐厅', 2000)).toBe('第二天加个博物馆 顺便推荐附近的餐厅')
  })
  it('拼接超长时截断到 maxLength', () => {
    expect(composeDraft('a'.repeat(10), 'b'.repeat(10), 15)).toBe(`${'a'.repeat(10)} ${'b'.repeat(4)}`)
  })
  it('空建议不打扰底稿', () => {
    expect(composeDraft('底稿', '   ', 2000)).toBe('底稿')
  })
})

describe('imageFeedbackText（识别失败文案）', () => {
  it('401 给登录引导', () => {
    expect(imageFeedbackText(new ReactApiError('请求失败（401）', 401))).toBe('登录后才能识别图片。')
  })
  it('413 给换图指引', () => {
    expect(imageFeedbackText(new ReactApiError('上传内容超过上限', 413))).toBe('图片太大了，换一张 5MB 以内的试试。')
  })
  it('网络/5xx 给重试提示', () => {
    expect(imageFeedbackText(new TypeError('fetch failed'))).toBe('暂时连不上识别服务，稍后再试。')
    expect(imageFeedbackText(new ReactApiError('bad gateway', 502))).toBe('暂时连不上识别服务，稍后再试。')
  })
  it('其余透传后端 message，无 message 时给兜底', () => {
    expect(imageFeedbackText(new ReactApiError('识别请求太频繁，稍后再试', 429))).toBe('识别请求太频繁，稍后再试')
    expect(imageFeedbackText(new Error(''))).toBe('这张图没能识别出来，换一张试试。')
  })
})

/** 语音交互链路：FakeRecognition 驱动（真 createRoot + act，零新测试依赖）。
 * 静态渲染测不到事件路径——本组用例钉住「接线」：识别回调确实写进输入框、
 * 单句结束自动复位、失败有可见反馈、发送后丢弃迟到的定稿回填。 */

interface FakeResult {
  isFinal: boolean
  length: number
  0: { transcript: string }
}

type RecognitionEvent = { resultIndex: number; results: FakeResult[] }

class FakeRecognition {
  static instances: FakeRecognition[] = []
  lang = ''
  continuous = true
  interimResults = false
  started = false
  stopped = false
  aborted = false
  onresult: ((event: RecognitionEvent) => void) | null = null
  onerror: ((event: { error: string }) => void) | null = null
  onend: (() => void) | null = null

  constructor() {
    FakeRecognition.instances.push(this)
  }

  start() {
    this.started = true
  }

  stop() {
    this.stopped = true
  }

  abort() {
    this.aborted = true
  }
}

const speechEvent = (results: Array<{ isFinal: boolean; transcript: string }>): RecognitionEvent => ({
  resultIndex: 0,
  results: results.map((item) => ({ isFinal: item.isFinal, length: 1, 0: { transcript: item.transcript } })),
})

async function mountComposer(onSend: (text: string) => void) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  const Harness = () => {
    const [draft, setDraft] = useState('想去成都')
    return createElement(ChatComposer, {
      ...baseProps,
      value: draft,
      onChange: setDraft,
      // 模拟真实调用方：提交即清空草稿
      onSend: () => {
        onSend(draft)
        setDraft('')
      },
    })
  }
  await act(async () => {
    root.render(createElement(Harness))
  })
  const input = () => container.querySelector('input[aria-label="对行程说话"]') as HTMLInputElement
  const micButton = () => container.querySelector('button[aria-label="语音输入"], button[aria-label="停止语音输入"]') as HTMLButtonElement
  const sendButton = () => container.querySelector('button[type="submit"]') as HTMLButtonElement
  return {
    input,
    micButton,
    sendButton,
    note: () => container.querySelector('.composer-note')?.textContent ?? '',
  }
}

describe('ChatComposer 语音交互（FakeRecognition）', () => {
  beforeEach(() => {
    ;(globalThis as unknown as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
    ;(window as unknown as Record<string, unknown>).SpeechRecognition = FakeRecognition
  })

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).SpeechRecognition
    FakeRecognition.instances = []
    document.querySelectorAll('div').forEach((node) => node.remove())
  })

  it('单击开始：zh-CN + 单句模式 + interim 实时上屏，定稿结束自动复位', async () => {
    const ui = await mountComposer(() => {})
    await act(async () => {
      ui.micButton().click()
    })
    const rec = FakeRecognition.instances.at(-1) as FakeRecognition
    expect(rec.started).toBe(true)
    expect(rec.lang).toBe('zh-CN')
    expect(rec.continuous).toBe(false)
    expect(rec.interimResults).toBe(true)

    // 真 Web Speech 语义：interim 是同一句的累积增长，final 是同句定稿（整体替换 interim）
    await act(async () => {
      rec.onresult?.(speechEvent([{ isFinal: false, transcript: '玩' }]))
    })
    expect(ui.input().value).toBe('想去成都玩')
    expect(ui.micButton().getAttribute('aria-label')).toBe('停止语音输入')
    expect(ui.note()).toContain('正在听')

    await act(async () => {
      rec.onresult?.(speechEvent([{ isFinal: false, transcript: '玩四天' }]))
    })
    expect(ui.input().value).toBe('想去成都玩四天')

    await act(async () => {
      rec.onresult?.(speechEvent([{ isFinal: true, transcript: '玩四天' }]))
      rec.onend?.()
    })
    expect(ui.input().value).toBe('想去成都玩四天')
    expect(ui.micButton().getAttribute('aria-label')).toBe('语音输入')
    expect(ui.note()).toBe('')
  })

  it('识别失败给出可见反馈，不残留监听态', async () => {
    const ui = await mountComposer(() => {})
    await act(async () => {
      ui.micButton().click()
    })
    const rec = FakeRecognition.instances.at(-1) as FakeRecognition
    await act(async () => {
      rec.onerror?.({ error: 'no-speech' })
      rec.onend?.()
    })
    expect(ui.note()).toContain('没听到内容')
    expect(ui.micButton().getAttribute('aria-label')).toBe('语音输入')
  })

  it('识别途中点发送：提交走原流程，迟到的定稿回填被丢弃', async () => {
    const sent: string[] = []
    const ui = await mountComposer((text) => sent.push(text))
    await act(async () => {
      ui.micButton().click()
    })
    const rec = FakeRecognition.instances.at(-1) as FakeRecognition
    await act(async () => {
      rec.onresult?.(speechEvent([{ isFinal: false, transcript: '玩四天' }]))
    })
    await act(async () => {
      ui.sendButton().click()
    })
    expect(sent).toEqual(['想去成都玩四天'])
    expect(ui.input().value).toBe('')
    // 发送触发 abort；浏览器随后补发 onend——旧底稿不该被写回刚清空的输入框
    await act(async () => {
      rec.onend?.()
    })
    expect(ui.input().value).toBe('')
  })
})
