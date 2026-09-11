/* 让浮窗能被拖走。
 *
 * 用法：给浮层根元素加 v-drag，拖它的 [data-drag] 子元素（没有就拖元素本身）。
 *   <div class="modal" v-drag>            <!-- 整个弹窗可拖 -->
 *   <div class="keys-card" v-drag="{ key: 'keys' }">   <!-- 位置记进 localStorage -->
 *   <div class="role-card" v-drag="{ onStart: () => (dragged = true) }">
 *
 * 三条实现约定：
 * 1. 位移写在 transform 上，不改 left/top——.desk-float 用的是 translate 属性、
 *    气泡用的是 left/top、弹窗靠 flex 居中，改 left/top 会把它们各自那套定位弄乱。
 * 2. 拖到哪儿都行，但至少留 48×40 露在视口里，别把自己拖没了。
 * 3. 真拖过（位移 > 4px）就吞掉随之而来的那次 click：拖完不该顺手把浮层关掉。
 */
const EDGE = 8

export const vDrag = {
  mounted(el, binding) {
    const opts = binding.value || {}
    let off = { x: 0, y: 0 }
    let st = null
    let moved = 0

    function apply() {
      el.style.transform = off.x || off.y ? `translate(${off.x}px, ${off.y}px)` : ''
    }

    // 量"没被 transform 推过"的基准位置：只在按下那一刻量，避开入场动画和后续重排
    function baseRect() {
      const keep = el.style.transform
      el.style.transform = 'none'
      const r = el.getBoundingClientRect()
      el.style.transform = keep
      return r
    }

    function clamp(x, y) {
      const b = baseRect()
      const maxX = Math.max(EDGE, window.innerWidth - Math.min(b.width, window.innerWidth) - EDGE)
      const maxY = Math.max(EDGE, window.innerHeight - Math.min(b.height, window.innerHeight) - EDGE)
      return {
        x: Math.min(Math.max(x, EDGE - b.left), Math.max(EDGE - b.left, maxX - b.left)),
        y: Math.min(Math.max(y, 0 - b.top), Math.max(0 - b.top, maxY - b.top)),
      }
    }

    function onMove(e) {
      if (!st) return
      const dx = e.clientX - st.px, dy = e.clientY - st.py
      moved = Math.max(moved, Math.abs(dx), Math.abs(dy))
      off = clamp(st.ox + dx, st.oy + dy)
      apply()
      e.preventDefault()
    }

    function swallow(e) { e.stopPropagation(); e.preventDefault() }

    function onUp() {
      window.removeEventListener('pointermove', onMove)
      el.classList.remove('dragging')
      st = null
      if (moved > 4) {
        // 拖完那一下不该被当成点击。父元素也要吞：弹窗被拖到遮罩上松手时，
        // click 的 target 是遮罩，遮罩上正好挂着"点外部关闭"。
        for (const node of [el, el.parentElement]) {
          if (!node) continue
          node.addEventListener('click', swallow, { capture: true, once: true })
          setTimeout(() => node.removeEventListener('click', swallow, true), 0)
        }
        if (opts.key) localStorage.setItem('eggpaper:drag:' + opts.key, JSON.stringify(off))
      }
      moved = 0
    }

    function onDown(e) {
      if (e.button !== 0 || e.target.closest('button, input, select, textarea, a, .no-drag')) return
      const handle = el.hasAttribute('data-drag') ? el : e.target.closest('[data-drag]')
      if (!handle) return
      st = { px: e.clientX, py: e.clientY, ox: off.x, oy: off.y }
      moved = 0
      el.classList.add('dragging')
      opts.onStart?.()
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp, { once: true })
      window.addEventListener('pointercancel', onUp, { once: true })
      e.preventDefault()
    }

    function onResize() { if (off.x || off.y) { off = clamp(off.x, off.y); apply() } }

    el.__dragDown = onDown
    el.__dragResize = onResize
    el.classList.add('draggable')
    if (opts.key) {
      try { off = JSON.parse(localStorage.getItem('eggpaper:drag:' + opts.key) || '') || off } catch { /* */ }
      if (off.x || off.y) { requestAnimationFrame(() => { off = clamp(off.x, off.y); apply() }) }
    }
    el.addEventListener('pointerdown', onDown)
    window.addEventListener('resize', onResize)
  },

  unmounted(el) {
    el.removeEventListener('pointerdown', el.__dragDown)
    window.removeEventListener('resize', el.__dragResize)
    el.__dragDown = el.__dragResize = null
  },
}
