import { describe, expect, it } from 'vitest'
import { mergeTranscript, speechSupported } from './speechText'

/** 语音输入纯函数：特性检测与增量合并（确定性，无浏览器实现依赖）。 */

const winWith = (extra: Record<string, unknown>) => extra as unknown as Window

describe('speechSupported', () => {
  it('无实现返回 false（Firefox 等：麦克风按钮不渲染，降级回打字）', () => {
    expect(speechSupported(winWith({}))).toBe(false)
  })
  it('标准或 webkit 前缀实现都算支持', () => {
    expect(speechSupported(winWith({ SpeechRecognition: class {} }))).toBe(true)
    expect(speechSupported(winWith({ webkitSpeechRecognition: class {} }))).toBe(true)
  })
})

describe('mergeTranscript', () => {
  it('空底稿直接返回识别文本（final 在前 interim 在后）', () => {
    expect(mergeTranscript('', '玩四天', '想去成都')).toBe('想去成都玩四天')
  })
  it('底稿保留、识别文本接在后面（中文直接拼接不补空格）', () => {
    expect(mergeTranscript('想去成都', '玩四天', '')).toBe('想去成都玩四天')
  })
  it('interim 是整体替换不是追加：同一底稿上新的 interim 覆盖旧值', () => {
    expect(mergeTranscript('底稿', '成都', '')).toBe('底稿成都')
    expect(mergeTranscript('底稿', '成都玩四天', '')).toBe('底稿成都玩四天')
  })
  it('final 定稿累计、interim 从定稿后继续接', () => {
    expect(mergeTranscript('底稿', '玩四天', '想去成都')).toBe('底稿想去成都玩四天')
  })
  it('ASCII 词元相接补空格，中文相接不补', () => {
    expect(mergeTranscript('go to Chengdu', 'four days', '')).toBe('go to Chengdu four days')
    expect(mergeTranscript('chengdu', 'four', 'I go to ')).toBe('chengdu I go to four')
  })
  it('无识别内容时底稿原样返回（含尾随空格，不做 trim 副作用）', () => {
    expect(mergeTranscript('底稿 ', '', '')).toBe('底稿 ')
  })
})
