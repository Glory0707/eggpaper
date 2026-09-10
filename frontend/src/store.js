import { reactive } from 'vue'
import { api } from './api'

export { api, ROLE_ZH, KIND_ZH, CORE_ROLES } from './api'

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
  viewer: { variant: 'original', layers: { skeleton: true, marginalia: true, skim: false } },
  jump: null,          // {page, y0, y1} 跳转信号
  toast: '',

  get mock() { return this.settings?.mock },
})

export function toast(msg) {
  store.toast = msg
  clearTimeout(toast._t)
  toast._t = setTimeout(() => (store.toast = ''), 2600)
}

export async function refreshPapers() {
  store.papers = await api.papers()
}

export async function openPaper(pid) {
  store.currentId = pid
  store.paper = await api.paper(pid)
  store.paras = await api.paragraphs(pid)
  store.summary = null
  store.qa = []
  refreshAnalysis()
  refreshMarginalia()
  api.qaHistory(pid).then(h => (store.qa = h)).catch(() => {})
  api.summary(pid).then(s => (store.summary = s)).catch(() => {})
}

export async function refreshAnalysis() {
  if (!store.currentId) return
  const a = await api.analysis(store.currentId)
  Object.assign(store.analysis, a)
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
