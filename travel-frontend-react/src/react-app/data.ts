import beijing from '../assets/img/cover-beijing.webp'
import bali from '../assets/img/cover-bali.webp'
import bangkok from '../assets/img/cover-bangkok.webp'
import chengdu from '../assets/img/cover-chengdu.webp'
import chongqing from '../assets/img/cover-chongqing.webp'
import dali from '../assets/img/cover-dali.webp'
import hangzhou from '../assets/img/cover-hangzhou.webp'
import kyoto from '../assets/img/cover-kyoto.webp'
import osaka from '../assets/img/cover-osaka.webp'
import paris from '../assets/img/cover-paris.webp'
import seoul from '../assets/img/cover-seoul.webp'
import shanghai from '../assets/img/cover-shanghai.webp'
import singapore from '../assets/img/cover-singapore.webp'
import tokyo from '../assets/img/cover-tokyo.webp'
import xiamen from '../assets/img/cover-xiamen.webp'
import xian from '../assets/img/cover-xian.webp'
import type { ItineraryDetail, ItinerarySummary } from '../types/itinerary'

export type DestinationCategory = '全部' | '人文' | '美食' | '自然' | '慢旅行'

export interface DestinationCard {
  city: string
  province: string
  image: string
  description: string
  categories: Exclude<DestinationCategory, '全部'>[]
  bestFor: string
  days: string
}

/** 城市候补（datalist 与匿名 chips 用）：与 destinations 同序，国内外交错（2026-10-03 拍板①）。 */
export const fallbackCities = ['成都', '东京', '杭州', '巴黎', '上海', '巴厘岛', '北京', '曼谷', '西安', '新加坡', '重庆', '京都', '大理', '首尔', '厦门', '大阪']

/** 目的地卡片：国内外交错排列，让首页展示区/问候 chips 的切片自然混排（2026-10-03 PLAN §2.5）。 */
export const destinations: DestinationCard[] = [
  { city: '成都', province: '四川', image: chengdu, description: '从一盏盖碗茶开始，把城市走慢一点。', categories: ['美食', '慢旅行'], bestFor: '川菜、街巷、茶馆', days: '3–5 天' },
  { city: '东京', province: '日本', image: tokyo, description: '从浅草的老街到新宿的天际线，反差本身就是风景。', categories: ['美食', '人文'], bestFor: '街区漫步、市集、城市夜景', days: '3–5 天' },
  { city: '杭州', province: '浙江', image: hangzhou, description: '湖光、茶山与一段不赶时间的江南日常。', categories: ['自然', '慢旅行'], bestFor: '湖景、茶园、散步', days: '2–4 天' },
  { city: '巴黎', province: '法国', image: paris, description: '左岸的咖啡与塞纳河的桥，把浪漫过成日常。', categories: ['人文', '美食'], bestFor: '博物馆、街角咖啡、甜点', days: '4–6 天' },
  { city: '上海', province: '上海', image: shanghai, description: '沿着梧桐树影，发现城市里安静的转角。', categories: ['人文', '美食'], bestFor: '建筑、展览、咖啡', days: '2–4 天' },
  { city: '巴厘岛', province: '印度尼西亚', image: bali, description: '梯田、海浪与神庙，把日子过成岛屿时间。', categories: ['自然', '慢旅行'], bestFor: '海滩、梯田、日落', days: '5–7 天' },
  { city: '北京', province: '北京', image: beijing, description: '在旧城的尺度里，听见一座城市的时间。', categories: ['人文', '美食'], bestFor: '古迹、博物馆、胡同', days: '3–5 天' },
  { city: '曼谷', province: '泰国', image: bangkok, description: '金顶与街边锅气同框，一座越夜越有味道的城市。', categories: ['美食', '人文'], bestFor: '街头小吃、集市、河岸夜色', days: '3–5 天' },
  { city: '西安', province: '陕西', image: xian, description: '一城古意，一路好吃，适合慢慢读懂。', categories: ['人文', '美食'], bestFor: '历史、面食、夜游', days: '3–4 天' },
  { city: '新加坡', province: '新加坡', image: singapore, description: '花园与高楼咬合，转角就有绿地与食阁。', categories: ['美食', '人文'], bestFor: '滨海湾、花园城市、食阁', days: '3–4 天' },
  { city: '重庆', province: '重庆', image: chongqing, description: '山城的坡度和烟火，组成一场有层次的漫游。', categories: ['美食', '人文'], bestFor: '火锅、夜景、城市漫游', days: '2–4 天' },
  { city: '京都', province: '日本', image: kyoto, description: '庭园、石板路与一寺一景，时间在这里放慢。', categories: ['人文', '慢旅行'], bestFor: '古寺、庭园、町屋街巷', days: '3–5 天' },
  { city: '大理', province: '云南', image: dali, description: '风从洱海来，日子可以只安排一半。', categories: ['自然', '慢旅行'], bestFor: '湖山、日落、发呆', days: '4–6 天' },
  { city: '首尔', province: '韩国', image: seoul, description: '宫殿与潮流街区一墙之隔，白天看山晚上逛店。', categories: ['美食', '人文'], bestFor: '街区小店、市场小吃', days: '3–4 天' },
  { city: '厦门', province: '福建', image: xiamen, description: '海风、骑楼和适合散步的傍晚。', categories: ['自然', '慢旅行'], bestFor: '海边、街区、骑行', days: '2–4 天' },
  { city: '大阪', province: '日本', image: osaka, description: '被称为厨房的城市，把吃喝写进了街头巷尾。', categories: ['美食', '人文'], bestFor: '市场、街食、城堡公园', days: '3–4 天' },
]

export interface InspirationTemplate {
  id: string
  title: string
  city: string
  theme: string
  description: string
  days: number
  image: string
  tags: string[]
  intent: string
}

/** 灵感模板：每城 1 条、与 destinations 同序；主题配平（美食4/城市漫游4/慢旅行3/自然3/亲子2），每主题 ≥2（2026-10-03 PLAN §2.5）。国际条目 intent 同口径：城市+天数+人数+偏好完整句。 */
export const inspirationTemplates: InspirationTemplate[] = [
  { id: 'chengdu-slow', title: '把成都走慢一点', city: '成都', theme: '慢旅行', description: '茶馆、老街和一顿不赶时间的川菜。', days: 3, image: chengdu, tags: ['少走路', '本地味道'], intent: '带父母去成都 3 天，少走路，喜欢川菜和老街，节奏松弛一些' },
  { id: 'tokyo-wander', title: '东京的街区与灯河', city: '东京', theme: '城市漫游', description: '上午逛老街市场，傍晚去看一次城市的灯河。', days: 4, image: tokyo, tags: ['街区', '夜景'], intent: '去东京 4 天，两个人，喜欢街区漫步和街边小吃，博物馆选一两个就好' },
  { id: 'hangzhou-tea', title: '西湖边的松弛周末', city: '杭州', theme: '自然', description: '湖边散步，去茶园坐一会儿，把周末留给风景。', days: 2, image: hangzhou, tags: ['湖景', '茶园'], intent: '去杭州 2 天，想看西湖和茶园，少安排赶路的景点' },
  { id: 'paris-taste', title: '巴黎的面包与河岸', city: '巴黎', theme: '美食', description: '把博物馆留给上午，午后交给面包房和塞纳河。', days: 5, image: paris, tags: ['甜点', '河岸'], intent: '两个人去巴黎 5 天，喜欢街角面包房和博物馆，想留时间沿河散步' },
  { id: 'shanghai-walk', title: '上海的梧桐与夜色', city: '上海', theme: '城市漫游', description: '从老建筑到夜市，顺着街区的纹理认识上海。', days: 3, image: shanghai, tags: ['建筑', '夜游'], intent: '去上海 3 天，喜欢老建筑、展览和夜间散步' },
  { id: 'bali-nature', title: '巴厘岛的梯田与海', city: '巴厘岛', theme: '自然', description: '上午看梯田，下午留给海滩，日落前赶到水边神庙。', days: 5, image: bali, tags: ['海滩', '梯田'], intent: '和朋友们去巴厘岛 5 天，想看梯田和海滩，喜欢日落，不想赶路' },
  { id: 'beijing-family', title: '带家人读北京', city: '北京', theme: '亲子', description: '故宫、胡同与一份适合全家人的从容节奏。', days: 4, image: beijing, tags: ['博物馆', '不赶路'], intent: '带家人去北京 4 天，想看博物馆和胡同，不要每天排太满' },
  { id: 'bangkok-street', title: '曼谷的街头锅气', city: '曼谷', theme: '美食', description: '白天看庙和集市，晚上把街头小吃当正餐。', days: 4, image: bangkok, tags: ['夜市', '街食'], intent: '去曼谷 4 天，四个人，想认真吃街头小吃，也逛一两个集市和寺庙' },
  { id: 'xian-food', title: '西安一城好吃', city: '西安', theme: '美食', description: '把历史放进白天，把面食和夜色留给晚上。', days: 3, image: xian, tags: ['面食', '古迹'], intent: '去西安 3 天，想看古迹，也想认真吃本地面食' },
  { id: 'singapore-family', title: '带孩子玩新加坡', city: '新加坡', theme: '亲子', description: '花园、动物园与食阁，适合全家的小尺度城市。', days: 4, image: singapore, tags: ['花园', '动物园'], intent: '带孩子去新加坡 4 天，想看花园和动物园，行程不要太赶' },
  { id: 'chongqing-night', title: '重庆的坡与灯', city: '重庆', theme: '城市漫游', description: '白天看山城层次，晚上去吃一顿热气腾腾的火锅。', days: 3, image: chongqing, tags: ['夜景', '火锅'], intent: '去重庆 3 天，想看夜景、吃火锅，安排一些城市漫游' },
  { id: 'kyoto-slow', title: '京都的一寺一庭', city: '京都', theme: '慢旅行', description: '每天只安排一两个去处，把时间留给庭园与石板路。', days: 3, image: kyoto, tags: ['古寺', '散步'], intent: '两个人去京都 3 天，喜欢古寺和庭园，想走得慢一些' },
  { id: 'dali-slow', title: '去大理过一半日子', city: '大理', theme: '慢旅行', description: '苍山洱海之间，把行程留白一半。', days: 4, image: dali, tags: ['洱海', '留白'], intent: '去大理 4 天，两个人，想看洱海和苍山，行程只安排一半，剩下随意' },
  { id: 'seoul-walk', title: '首尔的坡道与霓虹', city: '首尔', theme: '城市漫游', description: '白天逛传统市场，晚上沿汉江或街区走走。', days: 3, image: seoul, tags: ['街区', '夜游'], intent: '去首尔 3 天，两个人，喜欢逛街区和市场小吃，晚上想看夜景' },
  { id: 'xiamen-breeze', title: '厦门的海风傍晚', city: '厦门', theme: '自然', description: '环岛路的骑行和老城的巷子，把傍晚留给海。', days: 3, image: xiamen, tags: ['海边', '骑行'], intent: '去厦门 3 天，想看海边和老街区，喜欢傍晚散步，节奏松弛' },
  { id: 'osaka-food', title: '大阪，认真吃三天', city: '大阪', theme: '美食', description: '从市场到深夜小馆，把厨房之城吃出层次。', days: 4, image: osaka, tags: ['市场', '街食'], intent: '两个人去大阪 4 天，以吃为主线，也想沿河和城堡公园走走' },
]

export interface GuideContent {
  slug: string
  city: string
  title: string
  excerpt: string
  image: string
  readTime: string
  tag: string
  sections: { title: string; body: string }[]
}

export const guides: GuideContent[] = [
  { slug: 'chengdu-slow-city', city: '成都', title: '成都：把一天还给街巷与茶馆', excerpt: '一份以步行节奏为尺度的成都城市漫游建议。', image: chengdu, readTime: '6 分钟阅读', tag: '慢旅行', sections: [{ title: '先从一盏茶开始', body: '成都的好，不需要用打卡数量证明。把上午留给宽窄巷子和人民公园，找一张靠窗的桌子坐下来，观察这座城市如何把日常过得松弛。' }, { title: '给午后留一点空白', body: '下午可以沿着少城片区散步，穿过安静的街道，再把一顿川菜安排在傍晚。行程之间留出余量，才有机会遇到计划之外的小店。' }] },
  { slug: 'hangzhou-by-the-lake', city: '杭州', title: '杭州：西湖之外，茶山正好', excerpt: '把湖景、茶园和一段安静的下午放进两天周末。', image: hangzhou, readTime: '5 分钟阅读', tag: '自然', sections: [{ title: '湖边不必走完', body: '西湖很大，不需要用环湖一圈完成任务。选一段喜欢的湖岸，在树影下慢慢走，再把时间交给一间茶室。' }, { title: '去茶园呼吸', body: '龙井一带适合在上午前往，光线柔和，空气里有湿润的植物气息。回到市区后，留出一餐江南口味的晚饭。' }] },
  { slug: 'beijing-old-city', city: '北京', title: '北京：用一条胡同读懂旧城', excerpt: '故宫之外，旧城的尺度藏在一条条胡同的转弯里。', image: beijing, readTime: '7 分钟阅读', tag: '人文', sections: [{ title: '把博物馆留给上午', body: '北京的热门文化场馆需要提前核实开放时间和预约规则。上午看展，下午沿着胡同走一段，行程会更从容。' }, { title: '晚饭是另一种城市地图', body: '从小馆子到老字号，晚饭不必追求网红榜单。问问住处附近的人，往往能得到更贴近日常的答案。' }] },
  { slug: 'xian-under-the-wall', city: '西安', title: '西安：在城墙下，把历史走成日常', excerpt: '一份把古迹与市井面食串起来的西安慢走建议。', image: xian, readTime: '6 分钟阅读', tag: '人文', sections: [{ title: '上午把城墙和博物馆接起来', body: '把重点场馆放进上午，避开午后最晒的时段；热门场馆的预约规则请出发前核实。行程之间留出走路和吃面的余量。' }, { title: '晚上属于市井', body: '傍晚以后，城市把节奏交还给街巷。找一家本地人愿意排队的小馆，一碗面就是一餐；老城的夜色适合步行，多绕一条街常有收获。' }] },
  { slug: 'tokyo-block-by-block', city: '东京', title: '东京：在大都市里，走得像本地人', excerpt: '一份以街区为单位的东京漫游建议。', image: tokyo, readTime: '7 分钟阅读', tag: '城市漫游', sections: [{ title: '把一天拆成两个街区', body: '东京很大，不如每天选定一两个街区。上午逛老街区的市场，下午换一种气质，搭一段电车或沿河走走，城市会自己展开。' }, { title: '吃是东京的第二张地图', body: '从清晨的定食到深夜的小馆，餐饮密度极高。不执着于榜单，走进日常的店面常有惊喜；想去的名店，出发前再核实排队规则。' }] },
  { slug: 'paris-by-the-river', city: '巴黎', title: '巴黎：左岸的下午不用计划', excerpt: '把博物馆、河岸和街角咖啡串成一天。', image: paris, readTime: '6 分钟阅读', tag: '人文', sections: [{ title: '博物馆选一个，看深一点', body: '馆藏多到看不完。挑一两个真正想看的展馆留给上午，下午就会自由很多——城市本身就是展览的延长线；热门场馆的预约出发前核实。' }, { title: '河岸是巴黎的客厅', body: '天气好的傍晚，塞纳河两岸适合步行。带一点吃的坐在河岸石阶上看游船经过，比赶景点更像巴黎；个别步道会不定期整修，留意绕行。' }] },
]

export const sampleItinerary: ItineraryDetail = {
  id: 0,
  title: '成都 · 松弛四日',
  city: '成都',
  days: 4,
  stayNights: 3,
  persons: 2,
  budget: 3600,
  preferences: '美食；慢旅行；少走路',
  planNote: '示例行程：用于体验司南的编辑与地图工作台。',
  tripTheme: '把脚步放慢，把成都留给日常',
  status: 2,
  destinationStatus: 'draft_only',
  qualityStatus: 'DRAFT',
  pendingFactCount: 3,
  dayList: [
    { dayId: 1, dayNo: 1, theme: '从茶馆开始认识成都', note: '上午慢游，下午留白。', items: [{ itemType: 'attraction', poiName: '人民公园', poiId: null, address: '青羊区', latitude: 30.657, longitude: 104.066, startTime: '09:30', endTime: '11:30', durationMin: 120, openTime: null, cost: 0, tag: '茶馆', remark: '找一张靠窗的桌子坐一会儿', whyThis: '步行强度低，适合作为第一站', image: null, source: null, sourceUpdatedAt: null, verificationStatus: 'unverified', valueKind: 'estimated', freshnessStatus: 'unknown', reviewRequirement: 'before_departure', factEvidence: {} }, { itemType: 'food', poiName: '少城小馆', poiId: null, address: '少城片区', latitude: 30.666, longitude: 104.059, startTime: '12:00', endTime: '13:30', durationMin: 90, openTime: null, cost: 90, tag: '川菜', remark: '点两道招牌菜即可', whyThis: '把本地口味放进第一顿正餐', image: null, source: null, sourceUpdatedAt: null, verificationStatus: 'unverified', valueKind: 'estimated', freshnessStatus: 'unknown', reviewRequirement: 'before_departure', factEvidence: {} }], backupPlan: [], photoSpots: [], practicalNotes: ['营业时间与预约规则请在出发前核实'], dayOptions: [] },
    { dayId: 2, dayNo: 2, theme: '老街与一顿热辣川菜', note: '把景点和吃饭安排在一条顺路线上。', items: [{ itemType: 'attraction', poiName: '宽窄巷子', poiId: null, address: '青羊区', latitude: 30.662, longitude: 104.056, startTime: '10:00', endTime: '12:00', durationMin: 120, openTime: null, cost: 0, tag: '老街', remark: '避开正午人流', whyThis: '适合慢逛和拍照', image: null, source: null, sourceUpdatedAt: null, verificationStatus: 'unverified', valueKind: 'estimated', freshnessStatus: 'unknown', reviewRequirement: 'before_departure', factEvidence: {} }, { itemType: 'food', poiName: '本地川菜馆', poiId: null, address: '成都', latitude: null, longitude: null, startTime: '18:00', endTime: '19:30', durationMin: 90, openTime: null, cost: 120, tag: '晚餐', remark: '根据当天体力就近选择', whyThis: '晚间不再跨区移动', image: null, source: null, sourceUpdatedAt: null, verificationStatus: 'unverified', valueKind: 'estimated', freshnessStatus: 'unknown', reviewRequirement: 'before_departure', factEvidence: {} }], backupPlan: [], photoSpots: [], practicalNotes: [], dayOptions: [] },
    { dayId: 3, dayNo: 3, theme: '城市里的自然留白', note: '可根据天气在公园与室内之间切换。', items: [{ itemType: 'attraction', poiName: '杜甫草堂', poiId: null, address: '青羊区', latitude: null, longitude: null, startTime: '10:00', endTime: '12:00', durationMin: 120, openTime: null, cost: 50, tag: '人文', remark: '提前核实开放状态', whyThis: '庭院尺度适中，适合慢慢看', image: null, source: null, sourceUpdatedAt: null, verificationStatus: 'unverified', valueKind: 'estimated', freshnessStatus: 'unknown', reviewRequirement: 'before_departure', factEvidence: {} }], backupPlan: [], photoSpots: [], practicalNotes: [], dayOptions: [] },
    { dayId: 4, dayNo: 4, theme: '留给自己的一天', note: '不设必达点位，按兴趣补充。', items: [{ itemType: 'activity', poiName: '成都自由活动', poiId: null, address: '成都', latitude: null, longitude: null, startTime: '10:00', endTime: '17:00', durationMin: 420, openTime: null, cost: null, tag: '留白', remark: '可以回到喜欢的街区', whyThis: '留出调整空间，旅程不会被清单绑住', image: null, source: null, sourceUpdatedAt: null, verificationStatus: 'unverified', valueKind: 'generated', freshnessStatus: 'unknown', reviewRequirement: 'none', factEvidence: {} }], backupPlan: [], photoSpots: [], practicalNotes: [], dayOptions: [] },
  ],
  budgetList: [{ category: '交通', amount: 600, itemCount: 4 }, { category: '餐饮', amount: 1200, itemCount: 8 }, { category: '门票', amount: 300, itemCount: 3 }, { category: '住宿', amount: 1500, itemCount: 3 }],
  totalAmount: 3600,
  sources: [],
  suggestions: [],
}

export const sampleTripSummaries: ItinerarySummary[] = [
  { id: 0, title: sampleItinerary.title, city: '成都', days: 4, persons: 2, budget: 3600, totalAmount: 3600, status: 2, createdAt: '2026-09-20T09:00:00', startDate: null, endDate: null, favorite: true, archived: false, hasShare: false },
]
