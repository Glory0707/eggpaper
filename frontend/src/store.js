import { reactive, watch } from 'vue'
import { api } from './api'

export { api, ROLE_ZH, ROLE_GLYPH, KIND_ZH, CORE_ROLES, ROLE_COLOR, KIND_COLOR,
         ROLE_TEXT_COLOR, KIND_TEXT_COLOR, roleInk } from './api'

const LS = 'eggpaper:'

function lsGet(k, d) {
  try { return JSON.parse(localStorage.getItem(LS + k)) ?? d } catch { return d }
}
function lsSet(k, v) { localStorage.setItem(LS + k, JSON.stringify(v)) }

export const store = reactive({
  vw: window.innerWidth,     // 视口宽度：窄窗要换一套排布（右栏改浮层、栏位让给论文）
  papers: [],
  currentId: null,
  paper: null,
  paras: [],
  analysis: { status: 'none', claims: [], annotations: {}, evidence_qs: {}, error: '' },
  marginalia: { status: 'none', notes: [] },
  summary: null,
  qa: [],
  settings: null,
  viewer: {
    variant: lsGet('variant', 'original'),
    spread: lsGet('spread', 'spread'),
    layers: lsGet('layers', { skeleton: true, marginalia: true, skim: false }),
    care: lsGet('care', 'off'),            // 护眼底纹：off / mung / cyan / sand
    railUser: lsGet('railUser', true),     // 用户对右栏的偏好；双语对开姿势可临时覆盖
    frame: false,
    libOpen: false,
  },
  jump: null,            // {page, y0, y1, at}
  askPrefill: null,      // {paraIdx} 或 {text}
  glossaryPrefill: null,
  shortcutCard: false,
  askFocusTick: 0,
  escTick: 0,            // 按 Esc 递增：PDF 侧的浮层（划词/框选/角色卡）据此全部收起
  readingPara: null,     // 当前视口中心附近段落（scroll-spy）
  toast: '',
  viewerApi: null,       // PdfViewer 注册：{step, translateCurrent, jumpBack, translateSelectionKey}
  visPrefill: null,   // {img, question} 图表灯箱带过来的视觉问答        // 论证漫游停止器（RightRail 注册）

  get mock() { return this.settings?.mock },
  // 窄窗（半屏、竖屏、小笔记本）：右栏不再占版面，改成浮在书桌上的抽屉
  get narrow() { return this.vw < 1180 },
  get railRight() {
    if (this.viewer.variant === 'dual' && this.viewer.spread === 'spread') return false
    return this.viewer.railUser
  },
})

window.addEventListener('resize', () => { store.vw = window.innerWidth })

export function toast(msg) {
  store.toast = msg
  clearTimeout(toast._t)
  toast._t = setTimeout(() => (store.toast = ''), 2600)
}

watch(() => store.viewer.variant, v => lsSet('variant', v))
watch(() => store.viewer.spread, v => lsSet('spread', v))
watch(() => store.viewer.layers, v => lsSet('layers', v), { deep: true })
watch(() => store.viewer.railUser, v => lsSet('railUser', v))

/* 护眼底纹落在 <html> 上：CSS 变量在那里改，全站（含空态、弹层）一起换 */
function paintCare(v) {
  if (v && v !== 'off') document.documentElement.dataset.care = v
  else delete document.documentElement.dataset.care
}
watch(() => store.viewer.care, v => { lsSet('care', v); paintCare(v) }, { immediate: true })

export async function refreshPapers() {
  store.papers = await api.papers()
}

export async function openPaper(pid) {
  store.currentId = pid
  const pos = lsGet(`pos:${pid}`, {})
  if (pos.variant) store.viewer.variant = pos.variant
  if (pos.spread) store.viewer.spread = pos.spread
  store.paper = await api.paper(pid)
  store.paras = await api.paragraphs(pid)
  store.summary = null
  store.qa = []
  store.viewer.restorePos = pos.scroll || 0
  refreshAnalysis()
  refreshMarginalia()
  api.qaHistory(pid).then(h => (store.qa = h)).catch(() => {})
  api.summary(pid).then(s => (store.summary = s)).catch(() => {})
}

export async function refreshAnalysis() {
  if (!store.currentId) return
  const a = await api.analysis(store.currentId)
  Object.assign(store.analysis, a)
  store.paper = await api.paper(store.currentId)   // 同步 abbrs 等字段
  refreshPapers()
}

export async function refreshMarginalia() {
  if (!store.currentId) return
  const m = await api.marginalia(store.currentId)
  Object.assign(store.marginalia, m)
}

export function jumpTo(page, y0, y1) {
  store.jump = { page, y0, y1, at: Date.now() }
}
