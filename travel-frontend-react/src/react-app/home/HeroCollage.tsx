import type { CSSProperties } from 'react'
import { destinations } from '../data'
import type { DestinationCard } from '../data'

/** 首屏照片拼贴的城市（取画面最出片的 6 张，弱图不上首屏）；窄屏只露前 3 张。 */
const COLLAGE_CITIES = ['京都', '巴黎', '巴厘岛', '首尔', '厦门', '新加坡']

/** 导出仅供测试：拼贴必须全部命中 destinations（图源复用已登记署名的封面，见 CREDITS.md）。 */
export const HERO_COLLAGE: DestinationCard[] = COLLAGE_CITIES
  .map((city) => destinations.find((item) => item.city === city))
  .filter((item): item is DestinationCard => Boolean(item))

/** idle 首屏的目的地照片拼贴：纯装饰（aria-hidden、不可点，城市入口在下方展示区）。
 * 宽屏绝对定位分列对话卡两侧留白；窄屏收成标题上方的 3 张扇形小卡（布局全在 home.css）。
 * 前 3 张两种布局都在首屏，eager；后 3 张窄屏 display:none，lazy 让它们在窄屏不发请求。 */
export function HeroCollage() {
  return <div className="hero-collage" aria-hidden="true">
    {HERO_COLLAGE.map((item, index) => <figure key={item.city} className="hero-tile" style={{ '--i': index } as CSSProperties}>
      <img src={item.image} alt="" loading={index < 3 ? 'eager' : 'lazy'} decoding="async" />
      <figcaption>{item.city}</figcaption>
    </figure>)}
  </div>
}
