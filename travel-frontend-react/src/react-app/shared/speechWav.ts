/** 录音 → 16kHz 单声道 WAV：MiMo 的 ASR 只收 wav/mp3（2026-10-04 实证，见后端
 * speech_client.py），而 MediaRecorder 的默认产物是 webm——所以录制结果先在浏览器
 * 解码重编码再上传。16k 是语音 ASR 的原生采样率（MiMo-Audio 文档口径），同时把
 * 60 秒录音压到 ~1.9MB（8MB 上传上限内余量充足）。
 *
 * 解码失败/没有 AudioContext（旧浏览器、测试环境）时 `blobToWav16kMono` 抛错，
 * 调用方放弃转换、原样上传——由后端白名单给出可读的 400，而不是在这里吞掉。 */

export const WAV_SAMPLE_RATE = 16000

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i))
}

/** Float32 [-1,1] → 44 字节 RIFF 头 + Int16 PCM 单声道（纯函数，单测钉字节）。 */
export function encodeWav(samples: Float32Array, sampleRate: number = WAV_SAMPLE_RATE): Blob {
  const bytes = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(bytes)
  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true) // PCM 块长度
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // 单声道
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true) // byte rate = rate × 声道 × 2 字节
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // 位深
  writeAscii(view, 36, 'data')
  view.setUint32(40, samples.length * 2, true)
  let offset = 44
  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample))
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
    offset += 2
  }
  return new Blob([bytes], { type: 'audio/wav' })
}

/** 线性插值重采样（48k→16k 一类的整数倍场景；语音频带够用，不值得上低通滤波器）。 */
export function resampleLinear(input: Float32Array, fromRate: number, toRate: number = WAV_SAMPLE_RATE): Float32Array {
  if (fromRate === toRate) return input
  const outLength = Math.floor((input.length * toRate) / fromRate)
  const output = new Float32Array(outLength)
  for (let i = 0; i < outLength; i += 1) {
    const position = (i * fromRate) / toRate
    const index = Math.floor(position)
    const fraction = position - index
    const left = input[index] ?? 0
    const right = input[index + 1] ?? left
    output[i] = left + (right - left) * fraction
  }
  return output
}

type DecodedLike = {
  sampleRate: number
  length: number
  numberOfChannels: number
  getChannelData(channel: number): Float32Array
}

type AudioContextLike = {
  decodeAudioData(data: ArrayBuffer): Promise<DecodedLike>
  close(): Promise<void> | void
}

type AudioContextCtor = new () => AudioContextLike

/** 解码任意录音容器（webm/ogg/mp4）→ 降混单声道 → 16k → WAV Blob。 */
export async function blobToWav16kMono(blob: Blob, win: Window = window): Promise<Blob> {
  const Ctor = (win as Window & { AudioContext?: AudioContextCtor }).AudioContext
  if (!Ctor) throw new Error('当前浏览器没有 AudioContext，无法转 WAV')
  const context = new Ctor()
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer())
    let mono: Float32Array
    if (decoded.numberOfChannels <= 1) {
      mono = decoded.getChannelData(0).slice()
    } else {
      mono = new Float32Array(decoded.length)
      for (let channel = 0; channel < decoded.numberOfChannels; channel += 1) {
        const data = decoded.getChannelData(channel)
        for (let i = 0; i < decoded.length; i += 1) mono[i] += data[i] / decoded.numberOfChannels
      }
    }
    return encodeWav(resampleLinear(mono, decoded.sampleRate))
  } finally {
    void context.close()
  }
}
