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

/** 针对具体某一天的智能指令生成器 */
export function getDayQuickPrompts(dayNo: number, city?: string): QuickPromptItem[] {
  const cityPrefix = city ? `${city}·` : ''
  return [
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
