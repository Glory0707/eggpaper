/* 系统开了"减弱动态"（prefers-reduced-motion: reduce）时，JS 发起的平滑滚动也得跟着关。
   styles.css 里那条一刀切只能停掉 CSS 过渡/动画，管不到 scrollTo / scrollIntoView
   的 behavior——开着减弱动态的人，跳转时仍会看到一段长距离滚动动画。
   线程安全的前提：媒体查询随时可能被系统改，所以每次现查而不是缓存一次。 */
let mq = null
try { mq = window.matchMedia('(prefers-reduced-motion: reduce)') } catch { mq = null }

export const reduceMotion = () => !!(mq && mq.matches)

/* 给 scrollTo / scrollIntoView 的 behavior：平时 'smooth'，减弱动态时 'auto' */
export const scrollBehavior = (smooth = true) => (smooth && !reduceMotion() ? 'smooth' : 'auto')
