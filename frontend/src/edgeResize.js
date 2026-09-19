import { ref } from 'vue'

/* 左右栏共用的分栏拖宽手势。dir=1 往右拖变宽（文库），dir=-1 往左拖变宽（右栏）；
   拖动期间 body 挂 rail-resizing（全局光标 + 禁止选中），onEnd 给一次性重排的机会
   （右栏拖的过程中不重排论文，松手才重新定标）。 */
export function useEdgeResize({ get, set, min, max, def, dir = 1, persist = null, onEnd = null }) {
  const dragging = ref(false)
  let startX = 0, startW = 0

  const clamp = w => Math.round(Math.min(max, Math.max(min, w)))

  function start(e) {
    e.preventDefault()
    startX = e.clientX
    startW = get()
    dragging.value = true
    document.body.classList.add('rail-resizing')
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', end)
  }
  function onMove(e) {
    set(clamp(startW + dir * (e.clientX - startX)))
  }
  function end() {
    if (!dragging.value) return
    dragging.value = false
    document.body.classList.remove('rail-resizing')
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', end)
    if (persist) persist(get())
    onEnd?.()
  }
  function reset() {
    set(def)
    if (persist) persist(def)
    onEnd?.()
  }
  function nudge(d) {
    set(clamp(get() + d))
    onEnd?.()
  }
  return { dragging, start, end, reset, nudge }
}
