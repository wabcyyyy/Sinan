import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import { isOfflineError, isUnauthorized, ReactApiError } from '../../api/sinan'
import { Icon } from './Icon'
import { useSpeechInput } from './useSpeechInput'

/** 对话输入共用件：ChatIntake（首页）与 ChatPanel（详情）的 composer 结构同构。
 * 布局对标主流 LLM 对话框：上部舒适输入，底部工具栏（左侧图片识图/语音输入，右侧字符统计与发送）。
 * 支持动态轮播 placeholder 与平滑过渡。 */

export const TRAVEL_PROMPT_SUGGESTIONS = [
  '想去巴厘岛度蜜月，海边日落 + 悬崖 SPA，节奏慢一点…',
  '国庆去京都 5 天，想看枫叶古寺，尝尝怀石料理…',
  '周末两个人去厦门，吹吹海风吃海鲜，预算 2500…',
  '带父母去西安 4 天，不早起，偏好历史文化与地道小吃…',
  '巴黎 6 天深度游，卢浮宫、塞纳河游船与法式咖啡馆…',
  '带小朋友去三亚 4 天，住海边亲子酒店，挖沙踏浪…',
]

const IMAGE_MAX_BYTES = 5 * 1024 * 1024
const IMAGE_ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/webp']

export function ChatComposer({
  value,
  onChange,
  onSend,
  placeholder,
  ariaLabel,
  maxLength,
  className,
  disabled = false,
  onImage,
  imageBusy = false,
  onNotice,
  placeholderList,
}: {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  placeholder: string
  ariaLabel: string
  maxLength: number
  className: string
  disabled?: boolean
  /** 拿到已通过本地预检（类型/大小）的图片文件；上传与失败反馈由调用方经各自提示通道处理 */
  onImage?: (file: File) => void
  /** 图片识别进行中：图片按钮出 loading 态并禁用 */
  imageBusy?: boolean
  /** 本地预检失败（类型不符/超 5MB）的就地提示，调用方接到自己现有的提示通道 */
  onNotice?: (message: string) => void
  /** 定时轮播的占位符列表 */
  placeholderList?: string[]
}) {
  const fileRef = useRef<HTMLInputElement | null>(null)
  const speech = useSpeechInput({ getBase: () => value, onResult: onChange })
  const locked = disabled || imageBusy

  const list = placeholderList && placeholderList.length > 0 ? placeholderList : null
  const [cycleIndex, setCycleIndex] = useState(0)

  useEffect(() => {
    if (!list || list.length <= 1 || value) return
    const timer = setInterval(() => {
      setCycleIndex((prev) => (prev + 1) % list.length)
    }, 3800)
    return () => clearInterval(timer)
  }, [list, value])

  const activePlaceholder = list ? list[cycleIndex] : placeholder

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    // 清空选择值：同一张图（识别失败后重选）也要能再次触发 onChange
    event.target.value = ''
    if (!file) return
    if (!IMAGE_ACCEPTED_TYPES.includes(file.type)) {
      onNotice?.('只支持 JPG / PNG / WebP 图片。')
      return
    }
    if (file.size > IMAGE_MAX_BYTES) {
      onNotice?.('图片超过 5MB 了，换一张小一点的试试。')
      return
    }
    onImage?.(file)
  }

  return (
    <>
      <form
        className={className}
        onSubmit={(event) => {
          event.preventDefault()
          // 文本已发出，输入框即将清空：丢弃识别会话，迟到的定稿回填不该把旧底稿写回来
          speech.cancel()
          if (!disabled) onSend()
        }}
      >
        <div className="composer-input-row">
          <input
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                speech.cancel()
                if (!disabled && value.trim()) onSend()
              }
            }}
            placeholder={activePlaceholder}
            aria-label={ariaLabel}
            maxLength={maxLength}
            disabled={disabled || speech.listening}
          />
        </div>
        <div className="composer-toolbar">
          <div className="composer-tools-left">
            {onImage && (
              <>
                <input ref={fileRef} type="file" hidden accept="image/jpeg,image/png,image/webp" onChange={onFileChange} />
                <button
                  type="button"
                  className={imageBusy ? 'composer-image is-busy' : 'composer-image'}
                  aria-label={imageBusy ? '正在识别图片' : '识别图片'}
                  disabled={locked || speech.listening}
                  onClick={() => fileRef.current?.click()}
                  title="上传旅行/攻略图片识别意图"
                >
                  <Icon name="image" size={16} />
                  <span className="composer-tool-label">识图</span>
                </button>
              </>
            )}
            {speech.supported && (
              <button
                type="button"
                className={speech.listening ? 'composer-mic is-listening' : 'composer-mic'}
                aria-label={speech.listening ? '停止语音输入' : '语音输入'}
                aria-pressed={speech.listening}
                disabled={disabled || speech.transcribing}
                onClick={speech.toggle}
                title={speech.transcribing ? '正在转写刚才的语音…' : '语音输入想法'}
              >
                <Icon name="mic" size={16} />
                <span className="composer-tool-label">语音</span>
              </button>
            )}
          </div>
          <div className="composer-tools-right">
            {value.length > 0 && (
              <span className="composer-counter" aria-hidden="true">
                {value.length}/{maxLength}
              </span>
            )}
            <button type="submit" className="button button-primary composer-submit" disabled={disabled || !value.trim()} title="发送旅行想法">
              <span>发送</span>
              <Icon name="arrow" size={15} />
            </button>
          </div>
        </div>
      </form>
      {speech.error
        ? <p className="composer-note" role="alert">{speech.error}</p>
        : speech.listening
          ? <p className="composer-note" role="status">正在听你说，说完再点一下麦克风。</p>
          : speech.transcribing && <p className="composer-note" role="status">正在转写刚才说的内容…</p>}
    </>
  )
}

/** 把图片识别出的建议消息并进当前底稿：空底稿直接采用，非空以空格相接，超长截断。
 * 回填不自动发送——用户编辑后照常走原提交流程。 */
export function composeDraft(base: string, suggestion: string, maxLength: number): string {
  const head = base.trim()
  const add = suggestion.trim()
  if (!add) return base
  if (!head) return add.slice(0, maxLength)
  return `${head} ${add}`.slice(0, maxLength)
}

/** 图片识别失败的统一文案：401 给登录引导、413 给换图指引、离线给重试提示，
 * 其余透传后端 message（后端异常文案本身已脱敏）。 */
export function imageFeedbackText(error: unknown): string {
  if (isUnauthorized(error)) return '登录后才能识别图片。'
  if (error instanceof ReactApiError && error.status === 413) return '图片太大了，换一张 5MB 以内的试试。'
  if (isOfflineError(error)) return '暂时连不上识别服务，稍后再试。'
  return error instanceof Error && error.message ? error.message : '这张图没能识别出来，换一张试试。'
}
