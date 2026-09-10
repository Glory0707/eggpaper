import { reactive, watch } from 'vue'
import { api } from './api'

export { api, ROLE_ZH, KIND_ZH, CORE_ROLES, ROLE_COLOR, KIND_COLOR } from './api'

const LS = 'eggpaper:'

function lsGet(k, d) {
  try { return JSON.parse(localStorage.getItem(LS + k)) ?? d } catch { return d }
}
function lsSet(k, v) { localStorage.setItem(LS + k, JSON.stringify(v)) }

export const store = reactive({
  papers: [],
  currentId: null,
  paper: null,
  paras: [],
  analysis: { status: 'none', claims: [], annotations: {}, error: '' },
  marginalia: { status: 'none', notes: [] },
  summary: null,
  qa: [],
  settings: null,
  viewer: {
    variant: lsGet('variant', 'original'),
    spread: lsGet('spread', 'spread'),
    layers: lsGet('layers', { skeleton: true, marginalia: true, skim: false }),
    railUser: lsGet('railUser', true),     // 用户对右栏的偏好；双语对开姿势可临时覆盖
    libOpen: false,
  },
  jump: null,            // {page, y0, y1, at}
  askPrefill: null,      // {paraIdx} 或 {text}
  glossaryPrefill: null,
  shortcutCard: false,
  askFocusTick: 0,
  readingPara: null,     // 当前视口中心附近段落（scroll-spy）
  toast: '',
  viewerApi: null,       // PdfViewer 注册：{step, translateCurrent, jumpBack, translateSelectionKey}

  get mock() { return this.settings?.mock },
  get railRight() {
    return this.viewer.variant === 'dual' && this.viewer.spread === 'spread' ? false : this.viewer.railUser
  },
})

export function toast(msg) {
  store.toast = msg
  clearTimeout(toast._t)
  toast._t = setTimeout(() => (store.toast = ''), 2600)
}

watch(() => store.viewer.variant, v => lsSet('variant', v))
watch(() => store.viewer.spread, v => lsSet('spread', v))
watch(() => store.viewer.layers, v => lsSet('layers', v), { deep: true })
watch(() => store.viewer.railUser, v => lsSet('railUser', v))

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
