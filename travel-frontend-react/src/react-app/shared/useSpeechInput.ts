import { useCallback, useEffect, useRef, useState } from 'react'
import { interpretSpeechIntent, ReactApiError } from '../../api/sinan'
import { mergeTranscript, serverSpeechSupported, speechSupported } from './speechText'
import { blobToWav16kMono } from './speechWav'

/** lib.dom 未收编 SpeechRecognition：本地最小结构声明（只列用到的成员）。 */

interface SpeechAlternativeLike {
  transcript: string
}

interface SpeechResultLike {
  readonly length: number
  readonly isFinal: boolean
  [index: number]: SpeechAlternativeLike
}

interface SpeechResultListLike {
  readonly length: number
  [index: number]: SpeechResultLike
}

interface SpeechRecognitionEventLike {
  readonly resultIndex: number
  readonly results: SpeechResultListLike
}

interface SpeechRecognitionErrorEventLike {
  readonly error: string
}

interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  start(): void
  stop(): void
  abort(): void
  onresult: ((event: SpeechRecognitionEventLike) => void) | null
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null
  onend: (() => void) | null
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike

function recognitionCtor(win: Window): SpeechRecognitionCtor | null {
  const w = win as Window & { SpeechRecognition?: SpeechRecognitionCtor; webkitSpeechRecognition?: SpeechRecognitionCtor }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

const SPEECH_ERROR_TEXT: Record<string, string> = {
  'not-allowed': '麦克风权限被拒绝了，请在浏览器设置里允许后重试。',
  'service-not-allowed': '语音服务被浏览器拒绝了，请检查麦克风权限。',
  'no-speech': '没听到内容，再点一次试试。',
  'network': '语音服务暂时连不上，稍后再试。',
  'audio-capture': '没有找到可用的麦克风设备。',
  'language-not-supported': '当前浏览器不支持中文语音识别。',
}

const RECORD_FAILED_TEXT = '录音没能启动，请在浏览器设置里允许麦克风后重试。'
const RECORD_EMPTY_TEXT = '没录到内容，再点一次试试。'

export interface SpeechInputOptions {
  /** 按下麦克风那一刻的输入框底稿（识别期间作为合并基线，不随打字变化） */
  getBase: () => string
  /** 识别期间持续回调：参数是应显示的完整文本（底稿 + 转写），实时上屏 */
  onResult: (text: string) => void
}

/** 语音输入 hook：**服务端 ASR 优先，Web Speech 兜底**。
 *
 * 首选录音（MediaRecorder）→ 转 16k 单声道 WAV（MiMo 只收 wav/mp3，见 speechWav.ts）
 * → `POST /speech-intent`（服务端走 LLM_ROLE_STT，默认 MiMo），因为 Web Speech 依赖
 * 浏览器厂商的在线服务、国内网络下经常不可用。浏览器不能录音（happy-dom/旧浏览器/
 * 无麦克风 API）时退回 Web Speech，行为与 2026-10-04 之前一致。
 * 两者都没有 → supported=false，调用方据此不渲染麦克风按钮（降级路径是打字）。
 *
 * 单击开始/再击停止；服务端通道在停止后上传，识别结果回到 onResult。
 */
export function useSpeechInput({ getBase, onResult }: SpeechInputOptions) {
  const [listening, setListening] = useState(false)
  // FEUX-7：转写窗口态——停止录音到服务端转写返回之间（实测 1.45s，公网更久），
  // 没有它按钮立即回未激活、提示条消失，文本「凭空出现」
  const [transcribing, setTranscribing] = useState(false)
  const [error, setError] = useState('')
  const recRef = useRef<SpeechRecognitionLike | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const baseRef = useRef('')
  const finalRef = useRef('')
  const committedRef = useRef(-1)
  const discardRef = useRef(false)
  // 回调走 ref：识别会话横跨多次渲染，事件闭包必须总是拿到最新的回调
  const getBaseRef = useRef(getBase)
  const onResultRef = useRef(onResult)
  getBaseRef.current = getBase
  onResultRef.current = onResult

  const emitFinal = useCallback(() => {
    onResultRef.current(mergeTranscript(baseRef.current, '', finalRef.current))
  }, [])

  /** 停止录音并停掉音轨（不置 discard——onstop 里据此决定是否上传）。 */
  const stopRecorder = useCallback(() => {
    const recorder = recorderRef.current
    if (!recorder) return
    try {
      if (recorder.state !== 'inactive') recorder.stop()
    } catch {
      // 已停/未开：静默
    }
  }, [])

  const releaseStream = useCallback((recorder: MediaRecorder) => {
    recorder.stream?.getTracks().forEach((track) => track.stop())
  }, [])

  const upload = useCallback(async (blob: Blob) => {
    if (blob.size === 0) {
      setError(RECORD_EMPTY_TEXT)
      return
    }
    setTranscribing(true)
    // MiMo 只收 wav/mp3：webm 录音先转 16k 单声道 WAV；转不了（无 AudioContext/解码失败）
    // 就原样上传，让后端白名单给出可读 400——比在这里编一个假成功诚实。
    let file = new File([blob], 'clip.webm', { type: blob.type || 'audio/webm' })
    try {
      file = new File([await blobToWav16kMono(blob)], 'clip.wav', { type: 'audio/wav' })
    } catch {
      // 转换失败：保留原文件继续上传
    }
    try {
      const result = await interpretSpeechIntent(file)
      onResultRef.current(mergeTranscript(baseRef.current, '', result.suggestedMessage || result.text))
    } catch (err) {
      // 配置错（后端没配 LLM_ROLE_STT）与网络错都带可读 message：原样透出比吞掉有用
      setError(err instanceof ReactApiError || err instanceof Error ? err.message : '语音识别失败了，请再试一次。')
    } finally {
      setTranscribing(false)
    }
  }, [])

  const startRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const recorder = new MediaRecorder(stream)
      const chunks: Blob[] = []
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data && event.data.size > 0) chunks.push(event.data)
      }
      recorder.onstop = () => {
        releaseStream(recorder)
        recorderRef.current = null
        setListening(false)
        if (discardRef.current) return
        void upload(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }))
      }
      recorderRef.current = recorder
      recorder.start()
      setListening(true)
    } catch {
      recorderRef.current = null
      setListening(false)
      setError(RECORD_FAILED_TEXT)
    }
  }, [releaseStream, upload])

  const stop = useCallback(() => {
    if (recorderRef.current) {
      stopRecorder()
      return
    }
    try {
      recRef.current?.stop()
    } catch {
      // 未在识别态调 stop 会抛 InvalidStateError：静默即可
    }
  }, [stopRecorder])

  /** 丢弃本次识别并掐断会话：用于「识别途中用户已把文本发出去」——收尾的定稿回填
   * 会把刚清空的输入框写回旧底稿，必须跳过。 */
  const cancel = useCallback(() => {
    discardRef.current = true
    if (recorderRef.current) {
      stopRecorder()
      return
    }
    try {
      recRef.current?.abort()
    } catch {
      // 同上：静默
    }
  }, [stopRecorder])

  const toggle = useCallback(() => {
    if (recorderRef.current || recRef.current) {
      stop()
      return
    }
    // 转写窗口内不接新会话：上一段还没落字，叠录会让两段转写互相覆盖
    if (transcribing) return
    baseRef.current = getBaseRef.current()
    finalRef.current = ''
    committedRef.current = -1
    discardRef.current = false
    setError('')
    if (serverSpeechSupported()) {
      void startRecording()
      return
    }
    const Ctor = recognitionCtor(window)
    if (!Ctor) return
    const rec = new Ctor()
    rec.lang = 'zh-CN'
    rec.continuous = false
    rec.interimResults = true
    rec.onresult = (event) => {
      let interim = ''
      for (let i = 0; i < event.results.length; i += 1) {
        const result = event.results[i]
        if (result.isFinal) {
          // results 只增不改写：按索引去重，已累计的定稿段不重复追加
          if (i > committedRef.current) {
            finalRef.current += result[0]?.transcript ?? ''
            committedRef.current = i
          }
        } else {
          interim += result[0]?.transcript ?? ''
        }
      }
      onResultRef.current(mergeTranscript(baseRef.current, interim, finalRef.current))
    }
    rec.onerror = (event) => {
      // aborted 是主动停止/取消的伴生事件，不算失败
      if (event.error !== 'aborted') setError(SPEECH_ERROR_TEXT[event.error] ?? '语音识别失败了，请再试一次。')
    }
    rec.onend = () => {
      recRef.current = null
      setListening(false)
      if (!discardRef.current) emitFinal()
    }
    recRef.current = rec
    try {
      rec.start()
      setListening(true)
    } catch {
      recRef.current = null
      setError('语音识别没能启动，请再点一次试试。')
    }
  }, [emitFinal, startRecording, stop, transcribing])

  // 卸载时掐掉在途识别/录音；discard 让迟到的 onstop/onend 不再回填
  useEffect(
    () => () => {
      discardRef.current = true
      const recorder = recorderRef.current
      if (recorder) {
        try {
          if (recorder.state !== 'inactive') recorder.stop()
        } catch {
          // 静默
        }
        releaseStream(recorder)
      }
      try {
        recRef.current?.abort()
      } catch {
        // 静默
      }
    },
    [releaseStream],
  )

  return { supported: serverSpeechSupported() || speechSupported(), listening, transcribing, error, toggle, stop, cancel }
}
