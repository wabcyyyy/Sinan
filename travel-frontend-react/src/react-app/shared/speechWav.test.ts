import { afterEach, describe, expect, it } from 'vitest'
import { blobToWav16kMono, encodeWav, resampleLinear } from './speechWav'

/** 录音转 WAV（MiMo ASR 只收 wav/mp3）：头字节、重采样、降混与降级路径都是确定性的，
 * 逐字节钉住；decodeAudioData 用假 AudioContext 驱动（happy-dom 没有 Web Audio）。 */

async function wavBytes(blob: Blob): Promise<DataView> {
  return new DataView(await blob.arrayBuffer())
}

describe('encodeWav', () => {
  it('头 44 字节是标准 RIFF/WAVE PCM 单声道 16k/16bit', async () => {
    const blob = encodeWav(new Float32Array(4))
    const view = await wavBytes(blob)
    const ascii = (offset: number, length: number) =>
      String.fromCharCode(...Array.from({ length }, (_, i) => view.getUint8(offset + i)))
    expect(ascii(0, 4)).toBe('RIFF')
    expect(view.getUint32(4, true)).toBe(36 + 8) // 数据 4 样本 × 2 字节
    expect(ascii(8, 4)).toBe('WAVE')
    expect(ascii(12, 4)).toBe('fmt ')
    expect(view.getUint32(16, true)).toBe(16)
    expect(view.getUint16(20, true)).toBe(1) // PCM
    expect(view.getUint16(22, true)).toBe(1) // 单声道
    expect(view.getUint32(24, true)).toBe(16000)
    expect(view.getUint32(28, true)).toBe(32000)
    expect(view.getUint16(32, true)).toBe(2)
    expect(view.getUint16(34, true)).toBe(16)
    expect(ascii(36, 4)).toBe('data')
    expect(view.getUint32(40, true)).toBe(8)
    expect(view.byteLength).toBe(44 + 8)
    expect(blob.type).toBe('audio/wav')
  })

  it('Float32 → Int16：正负满幅、零与超界钳制', async () => {
    const view = await wavBytes(encodeWav(Float32Array.of(0, 1, -1, 2)))
    expect(view.getInt16(44, true)).toBe(0)
    expect(view.getInt16(46, true)).toBe(0x7fff)
    expect(view.getInt16(48, true)).toBe(-0x8000)
    expect(view.getInt16(50, true)).toBe(0x7fff) // 超界钳到满幅
  })
})

describe('resampleLinear', () => {
  it('48k→16k 长度缩为 1/3，整数倍率即隔点取样', () => {
    const input = Float32Array.of(0, 0.25, 0.5, 0.75, 1, 1.25)
    const output = resampleLinear(input, 48000)
    expect(output.length).toBe(2)
    expect(output[0]).toBeCloseTo(0)
    expect(output[1]).toBeCloseTo(0.75)
  })

  it('非整数倍率走线性插值（22050→16000）', () => {
    const input = Float32Array.of(0, 1, 0, 1)
    const output = resampleLinear(input, 22050)
    expect(output.length).toBe(2)
    expect(output[0]).toBeCloseTo(0)
    // 位置 1.378125：input[1]=1 与 input[2]=0 之间插值
    expect(output[1]).toBeCloseTo(0.621875, 5)
  })

  it('同采样率原样返回（不拷贝不重排）', () => {
    const input = new Float32Array([0.5, -0.5])
    expect(resampleLinear(input, 16000)).toBe(input)
  })
})

describe('blobToWav16kMono', () => {
  const original = (window as unknown as Record<string, unknown>).AudioContext

  afterEach(() => {
    if (original === undefined) delete (window as unknown as Record<string, unknown>).AudioContext
    else (window as unknown as Record<string, unknown>).AudioContext = original
  })

  it('立体声降混单声道再重采样（L=1,R=-1 → 静音；48k→16k）', async () => {
    const samples = 6
    ;(window as unknown as Record<string, unknown>).AudioContext = class {
      async decodeAudioData(): Promise<unknown> {
        return {
          sampleRate: 48000,
          length: samples,
          numberOfChannels: 2,
          getChannelData: (channel: number) =>
            Float32Array.from({ length: samples }, () => (channel === 0 ? 1 : -1)),
        }
      }
      close() {}
    }
    const blob = await blobToWav16kMono(new Blob(['webm-bytes']))
    const view = await wavBytes(blob)
    expect(view.getUint32(24, true)).toBe(16000)
    expect(view.getUint32(40, true)).toBe(4) // 6 样本 → 16k 下 2 样本 × 2 字节
    expect(view.getInt16(44, true)).toBe(0)
    expect(view.getInt16(46, true)).toBe(0)
  })

  it('没有 AudioContext 时抛错（调用方放弃转换、原样上传交后端白名单把关）', async () => {
    delete (window as unknown as Record<string, unknown>).AudioContext
    await expect(blobToWav16kMono(new Blob(['x']), window)).rejects.toThrow('AudioContext')
  })
})
