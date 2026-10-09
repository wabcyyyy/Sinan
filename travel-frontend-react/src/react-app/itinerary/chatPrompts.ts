/**
 * 智能快捷微调指令池与上下文感知派生（2026-10-03 对话改一切）。
 * 根据用户当前查看的天数与城市，生成场景化的精炼自然语言指令，
 * 降低用户打字负担，一键触发对话编排与 diff 确认卡。
 */

export interface QuickPromptItem {
  id: string
  category: 'route' | 'pace' | 'food' | 'spot' | 'hotel'
  categoryLabel: string
  label: string
  prompt: string
  icon?: string
}

/** M6（spec §12 快捷建议过滤）：近期对话上下文。FE 本地历史即可满足"明确不要的东西
 * 不再推荐"——requirements_struct 是 BE 内部状态，不为其新增公开投影。 */
export interface QuickPromptOptions {
  /** 近几条对话消息（user/ai 原文均可：AI 的确认回复会复述用户原话） */
  recentMessages?: Array<{ role: string; content: string }>
}

/** 只扫最近 N 条消息：更早的推翻性指令（"又想去博物馆了"）不该再拉黑建议 */
const EXCLUSION_SCAN_WINDOW = 6

/** 否定短语 + 紧随的排除对象（2-12 个非标点字符）：「不要博物馆」「不去西湖」「别安排美术馆」 */
const NEGATION_RE = /(?:不要|不想去|不去|别去|别安排|不要安排)\s*([^，。；、！？!?,.\s]{2,12})/g

/**
 * 从近期对话里提取排除项（M6）：用户说过「不要 X」后，快捷建议不再推荐含 X 的条目。
 * 纯本地词面匹配，不做语义理解——识别不了的不过滤（宁可多推荐，不可违背明确排除）。
 */
export function excludedTermsFromMessages(messages: Array<{ role: string; content: string }> | undefined): string[] {
  if (!messages?.length) return []
  const terms = new Set<string>()
  for (const message of messages.slice(-EXCLUSION_SCAN_WINDOW)) {
    const content = typeof message?.content === 'string' ? message.content : ''
    for (const match of content.matchAll(NEGATION_RE)) {
      terms.add(match[1])
    }
  }
  return [...terms]
}

/** 针对具体某一天的智能指令生成器 */
export function getDayQuickPrompts(dayNo: number, city?: string, options?: QuickPromptOptions): QuickPromptItem[] {
  const cityPrefix = city ? `${city}·` : ''
  const prompts: QuickPromptItem[] = [
    {
      id: `route-${dayNo}`,
      category: 'route',
      categoryLabel: '路线',
      label: `优化第 ${dayNo} 天顺路路线`,
      prompt: `请优化第 ${dayNo} 天的游览顺序，尽量按地理空间顺路排列，减少往返折返。`,
    },
    {
      id: `pace-${dayNo}`,
      category: 'pace',
      categoryLabel: '节奏',
      label: `把第 ${dayNo} 天节奏放轻松`,
      prompt: `第 ${dayNo} 天的安排有点赶，请把节奏调得更悠闲一点，为自由活动和拍照留足时间。`,
    },
    {
      id: `afternoon-tea-${dayNo}`,
      category: 'food',
      categoryLabel: '体验',
      label: `第 ${dayNo} 天下午加个特色下午茶`,
      prompt: `请在第 ${dayNo} 天下午安排一家${cityPrefix}高口碑的特色茶馆或咖啡馆，稍作歇息。`,
    },
    {
      id: `dinner-${dayNo}`,
      category: 'food',
      categoryLabel: '美食',
      label: `第 ${dayNo} 天晚餐换成地道特色菜`,
      prompt: `请把第 ${dayNo} 天的晚餐推荐换成一家地道${cityPrefix}特色餐厅或必吃榜美食。`,
    },
    {
      id: `museum-${dayNo}`,
      category: 'spot',
      categoryLabel: '景点',
      label: `第 ${dayNo} 天换一个室内场馆`,
      prompt: `如果遇到下雨或天气太热，请把第 ${dayNo} 天其中一个户外景点换成当地高评价的博物馆或艺术馆。`,
    },
    {
      id: `hotel-${dayNo}`,
      category: 'hotel',
      categoryLabel: '住宿',
      label: `推荐第 ${dayNo} 天附近的高分酒店`,
      prompt: `请推荐第 ${dayNo} 天游览区域附近交通便利、口碑好的品质酒店或民宿候选。`,
    },
  ]
  const excluded = excludedTermsFromMessages(options?.recentMessages)
  if (!excluded.length) return prompts
  // 「不要博物馆」→ 含"博物馆"的建议整条隐藏（label/prompt 任一命中即过滤）
  return prompts.filter(
    (item) => !excluded.some((term) => item.prompt.includes(term) || item.label.includes(term)),
  )
}

/** 详情页空状态下的精选分类场景指南 */
export interface PromptSceneGroup {
  sceneTitle: string
  prompts: string[]
}

export function getPromptSceneGroups(dayNo = 1, city?: string): PromptSceneGroup[] {
  const cityTag = city ? `在${city}` : ''
  return [
    {
      sceneTitle: '🗺️ 路线与节奏顺心',
      prompts: [
        `优化第 ${dayNo} 天顺路路线，减少折返`,
        `把第 ${dayNo} 天安排更松弛一些`,
        `第 1 天下午想早点入住休息，减少一个点位`,
      ],
    },
    {
      sceneTitle: '🍜 舌尖与生活体验',
      prompts: [
        `把第 ${dayNo} 天晚餐换成当地特色必吃餐厅`,
        `下午安排一个适合歇脚拍照的咖啡馆`,
        `${cityTag}晚上有没有值得逛的夜市或小吃街？`,
      ],
    },
    {
      sceneTitle: '🏛️ 景点与场馆调整',
      prompts: [
        `第 ${dayNo} 天加一个适合亲子或安静的室内场馆`,
        `换一个更有当地历史风貌的老街或古镇`,
        `去掉商业化严重的景点，换更小众出片的去处`,
      ],
    },
  ]
}
