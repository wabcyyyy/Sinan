/** 语音输入的纯函数层（确定性与单测）：浏览器特性检测 + 底稿/识别文本的增量合并。
 * Web Speech API 的构造器与事件类型 lib.dom 未收编，最小声明在 useSpeechInput.ts。 */

type SpeechCapableWindow = Window & { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown }

/** 浏览器是否带 Web Speech 识别实现（Chrome/Edge/Safari 可用）。
 * Firefox 等不支持的环境返回 false——此时由服务端 ASR 兜底（见 serverSpeechSupported）。 */
export function speechSupported(win: Window = window): boolean {
  const w = win as SpeechCapableWindow
  return Boolean(w.SpeechRecognition || w.webkitSpeechRecognition)
}

type RecorderCapableWindow = Window & {
  MediaRecorder?: unknown
  navigator: Navigator & { mediaDevices?: { getUserMedia?: unknown } }
}

/** 浏览器能否录音并把音频交给**服务端 ASR**（LLM_ROLE_STT，默认 MiMo）。
 *
 * 这是 2026-10-04 起的**首选**语音通道：Web Speech 依赖浏览器厂商的在线服务，
 * 国内网络下经常不可用，失败时用户只看到"语音服务暂时连不上"。录音走服务端后，
 * 词表/口音与可用性都掌握在自己手里；Web Speech 退化成不支持录音时的兜底。
 * 拿不到麦克风（无 mediaDevices）或没有 MediaRecorder 时返回 false。 */
export function serverSpeechSupported(win: Window = window): boolean {
  const w = win as RecorderCapableWindow
  return Boolean(w.MediaRecorder && w.navigator?.mediaDevices?.getUserMedia)
}

/** ASCII 词元相接需要空格隔开；中文转写之间直接拼接（zh-CN 转写本身不带空格）。 */
function needsSpaceBetween(left: string, right: string): boolean {
  return /[A-Za-z0-9]$/.test(left) && /^[A-Za-z0-9]/.test(right)
}

/** 底稿 + 进行中转写（interim）+ 已定稿转写（final）→ 输入框应显示的完整文本。
 * base 是按下麦克风那一刻的底稿（识别期间不变）；final 是已定稿段的累计追加；
 * interim 是当前未定稿片段，每次识别事件**整体替换**而不是追加。空段不参与拼接，
 * 段间衔接按词元补空格（转写自带的首尾空格会被裁掉，不能靠原文空格）。 */
export function mergeTranscript(base: string, interim: string, final: string): string {
  const head = base.trimEnd()
  const finalPart = final.trim()
  const interimPart = interim.trim()
  let spoken = finalPart
  if (interimPart) {
    spoken = spoken ? spoken + (needsSpaceBetween(spoken, interimPart) ? ' ' : '') + interimPart : interimPart
  }
  if (!head) return spoken
  if (!spoken) return base
  return head + (needsSpaceBetween(head, spoken) ? ' ' : '') + spoken
}
