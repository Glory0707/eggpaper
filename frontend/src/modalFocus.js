/* 弹层焦点：打开时焦点移入弹层本体（Tab 自然进入控件），Tab 在弹层内打圈不出界，
   关闭时焦点归还触发者。两种用法：
   - 组件随 v-if 挂载/卸载（SettingsModal）：modalFocus(rootRef)
   - 组件常驻、内部 v-if 开关（CiteCard/UpdateCard）：modalFocus(rootRef, openRef)
   - 第三参给初始焦点（ref 或 getter）：Dialog 的输入框/主按钮 */
import { onMounted, onBeforeUnmount, watch } from 'vue'

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'

export function modalFocus(rootRef, openRef, initialFocus = null) {
  let prev = null
  let off = null
  function attach() {
    if (off || !rootRef.value) return
    prev = document.activeElement
    const root = rootRef.value
    if (!root.hasAttribute('tabindex')) root.setAttribute('tabindex', '-1')
    root.style.outline = 'none'
    // 初始焦点：弹层里有更合适的第一落点（输入框/主按钮）就先落那儿，Tab 圈闭照常工作
    const first = (typeof initialFocus === 'function' ? initialFocus() : initialFocus?.value) || root
    first.focus({ preventScroll: true })
    const onKey = (e) => {
      if (e.key !== 'Tab') return
      const items = [...root.querySelectorAll(FOCUSABLE)].filter(el => el.offsetParent !== null)
      if (!items.length) return
      const first = items[0], last = items[items.length - 1]
      const cur = document.activeElement
      if (e.shiftKey && (cur === first || cur === root)) { e.preventDefault(); last.focus({ preventScroll: true }) }
      else if (!e.shiftKey && (cur === last || cur === root || !root.contains(cur))) { e.preventDefault(); first.focus({ preventScroll: true }) }
    }
    root.addEventListener('keydown', onKey)
    off = () => {
      root.removeEventListener('keydown', onKey)
      if (prev && typeof prev.focus === 'function') prev.focus({ preventScroll: true })
    }
  }
  function detach() { off?.(); off = null }

  if (openRef) {
    watch(openRef, v => {
      if (v) requestAnimationFrame(attach)
      else detach()
    }, { flush: 'post' })
  } else {
    onMounted(() => requestAnimationFrame(attach))
  }
  onBeforeUnmount(detach)
}
