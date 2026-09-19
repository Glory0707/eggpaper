import { reactive, watch, computed } from 'vue'
import { api } from './api'
import { t, ui } from './i18n'

/* 只转出组件真的会 import 的那些。颜色/标签的字典（KIND_*、BAND_*）不再对外——
   它们只该通过下面这三个解析口被读到，散出去就又会有人绕过口径直接取色。 */
export { api, askStream, ROLE_ZH, ROLE_COLOR, ROLE_TEXT_COLOR,
         bandOf, kindColor, kindText, kindZH } from './api'

const LS = 'eggpaper:'

export function lsGet(k, d) {
  try { return JSON.parse(localStorage.getItem(LS + k)) ?? d } catch { return d }
}
export function lsSet(k, v) { localStorage.setItem(LS + k, JSON.stringify(v)) }

export const store = reactive({
  vw: window.innerWidth,     // 视口宽度：窄窗要换一套排布（右栏改浮层、栏位让给论文）
  papers: [],
  currentId: null,
  paper: null,
  paras: [],
  analysis: { status: 'none', claims: [], annotations: {}, evidence_qs: {}, error: '' },
  marginalia: { status: 'none', notes: [], progress: null },
  summary: null,
  summaryErr: '',
  settings: null,
  lib: { colls: [], map: {}, coll: 'all', q: '', sort: lsGet('libSort', 'added') },
  viewer: {
    variant: lsGet('variant', 'original'),
    spread: lsGet('spread', 'spread'),
    layers: { marginalia: true, mine: true, ...lsGet('layers', {}) },
    care: lsGet('care', 'off'),            // 护眼底纹：off / mung / cyan / sand
    fs: lsGet('fs', 'std'),                // 字号：sm / std / lg / xl（论文正文不受影响）
    railUser: lsGet('railUser', true),     // 用户对右栏的偏好；双语对开姿势可临时覆盖
    railW: lsGet('railW', 336),            // 右栏宽度：可拖可双击复位
    noteBands: lsGet('noteBands', { good: true, warn: true, noise: true, mine: true }),
    frame: false,
    libOpen: false,
    calOpen: false,        // 论文日历抽屉（与文库同侧，互斥打开）
    tocOpen: false,        // 目录抽屉（同一侧第三层）
  },
  jump: null,            // {page, y0, y1, at}
  cite: { open: false }, // 「引用」浮层：开在顶栏标题旁，内容由 CiteCard 自己拉
  update: {
    show: false, current: '', latest: '', notes: '', url: '', size: 0, sha256: '',
    pub_date: '', required: false, packaged: false, installing: false,
    prog: { state: 'idle', pct: 0, got: 0, total: 0, path: '', error: '' },
  },
  askPrefill: null,      // {paraIdx} 或 {text}
  glossaryPrefill: null,
  shortcutCard: false,
  askFocusTick: 0,
  escTick: 0,            // 按 Esc 递增：PDF 侧的浮层（划词/框选/查找）据此全部收起
  readingPara: null,     // 当前视口中心附近段落（scroll-spy）
  reflowTick: 0,          // 栏宽拖完递增一次：论文据此重新定标（拖的过程中不重排）
  toast: '',
  epoch: 0,              // 换一篇 +1：按篇的异步请求回来时对不上就丢掉（见 openPaper）
  viewerApi: null,       // PdfViewer 注册：{step, translateCurrent, jumpBack, translateSelectionKey}
  visPrefill: null,   // {img, question} 图表灯箱带过来的视觉问答

  get narrow() { return this.vw < 1180 },
  get railRight() {
    if (this.viewer.variant === 'dual' && this.viewer.spread === 'spread') return false
    return this.viewer.railUser
  },
})

window.addEventListener('resize', () => { store.vw = window.innerWidth })

export function toast(msg, ms = 2600) {
  store.toast = t(msg)
  clearTimeout(toast._t)
  toast._t = setTimeout(() => (store.toast = ''), ms)
}

/* 纯英文模式不带翻译模块：变体永远停在原文。存过的偏好不改（回中文还在），
   只在"要往回读"的地方拦住它。 */
watch(() => ui.lang, l => { if (l === 'en' && store.viewer.variant !== 'original') store.viewer.variant = 'original' })

watch(() => store.viewer.variant, v => lsSet('variant', v))
watch(() => store.viewer.spread, v => lsSet('spread', v))
watch(() => store.viewer.layers, v => lsSet('layers', v), { deep: true })
watch(() => store.viewer.railUser, v => lsSet('railUser', v))
watch(() => store.viewer.railW, v => lsSet('railW', v))
watch(() => store.viewer.noteBands, v => lsSet('noteBands', v), { deep: true })
watch(() => store.lib.sort, v => lsSet('libSort', v))

/* 护眼底纹落在 <html> 上：CSS 变量在那里改，全站（含空态、弹层）一起换 */
function paintCare(v) {
  if (v && v !== 'off') document.documentElement.dataset.care = v
  else delete document.documentElement.dataset.care
}
watch(() => store.viewer.care, v => { lsSet('care', v); paintCare(v) }, { immediate: true })

/* 字号：设置里四档，落成 <html> 上的一个 --fs-scale。全站的六个字号 token 都是
   calc(基准 * var(--fs-scale))，乘一次全都跟着走；**论文正文不动**——纸上那层字是
   pdf.js 按视口比例写死的内联 font-size，不认 CSS 变量。 */
export const FS_SCALE = { sm: 0.93, std: 1, lg: 1.09, xl: 1.2 }
function paintFs(k) {
  const s = FS_SCALE[k] ?? 1
  if (s === 1) document.documentElement.style.removeProperty('--fs-scale')
  else document.documentElement.style.setProperty('--fs-scale', String(s))
}
watch(() => store.viewer.fs, k => {
  lsSet('fs', k)
  paintFs(k)
  store.reflowTick++     // 页边书签、旁批、沟槽宽度是量出来的，字号一变要重新量
}, { immediate: true })

/* 查更新。silent=true（默认）时一切失败都咽下去——这是打开软件时的一次安静探测，
   源没配、网断了、源上没东西，都不该变成一个报错弹窗。 */
export async function checkUpdate(force = false, silent = true) {
  try {
    const r = await api.updateCheck(force)
    if (r.has_update) store.update = { ...store.update, ...r, show: true }
    return r
  } catch (e) {
    if (!silent) toast(t('检查更新失败：{m}', { m: e.message }))
    return null
  }
}

export async function loadVersion() {
  try {
    const v = await api.version()
    store.update = { ...store.update, current: v.version, packaged: v.packaged }
    return v
  } catch { return null }
}

export async function refreshPapers() {
  store.papers = await api.papers()
}

/* idx → 段落 的表。三个组件（纸面 / 右栏 / 提问）都要按 ¶n 找段落，
   原来各建一份——同一份数据在一篇论文里被 entries 三遍。收成一处，换篇只算一次。 */
export const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))

export async function refreshCollections() {
  const r = await api.collections()
  store.lib.colls = r.collections
  store.lib.map = r.map
  if (store.lib.coll !== 'all' && store.lib.coll !== 'none' && !r.collections.some(c => c.id === store.lib.coll)) {
    store.lib.coll = 'all'      // 选中的分类被删了：回"全部"，别停在空列表上
  }
}

/* 按篇请求的统一口径：发之前记下"现在是哪一篇"（store.epoch），回来时对不上就丢掉。
   为什么要有它：一眼卡/五问/导师三问/方法卡都是**秒级**的模型调用，用户"打开 A 看一眼
   就点 B"时，A 的答案会落在 B 上（B 的速览页显示 A 的发现、A 的角色套到 B 的段落上），
   而 store.openPaper 是手写清场的——漏一个字段就漏一个洞。 */
export function paperEpoch() { return store.epoch }
export function samePaper(mine) { return store.epoch === mine }

/* 回书桌：当前论文清场、文库不动。阅读位置本来就存在本地，回来随时接上。
   这也是「主页」的正式入口：字标点击 / g h / 启动时按上次的记录。 */
export function goHome() {
  lsSet('lastPaper', '')
  store.currentId = null
  store.epoch++
  store.paper = null
  store.paras = []
  store.analysis = { status: 'none', claims: [], annotations: {}, evidence_qs: {}, error: '' }
  store.marginalia = { status: 'none', notes: [], progress: null }
  store.readingPara = null
  store.summary = null
  store.summaryErr = ''
}

export async function openPaper(pid) {
  lsSet('lastPaper', pid)
  store.currentId = pid
  store.epoch++
  const pos = lsGet(`pos:${pid}`, {})
  if (pos.variant) store.viewer.variant = pos.variant
  if (pos.spread) store.viewer.spread = pos.spread
  if (ui.lang === 'en') store.viewer.variant = 'original'   // 英文模式没有译文/双语
  store.paper = await api.paper(pid)
  if (store.viewer.variant !== 'original' && store.paper.translate_status !== 'done') {
    store.viewer.variant = 'original'
  }
  store.paras = []
  store.analysis = { status: 'none', claims: [], annotations: {}, evidence_qs: {}, error: '' }
  store.marginalia = { status: 'none', notes: [], progress: null }
  store.readingPara = null
  store.paras = await api.paragraphs(pid)
  store.summary = null
  store.summaryErr = ''
  store.viewer.restorePos = pos.scroll || 0
  refreshAnalysis()
  refreshMarginalia()
  const mine = store.epoch
  api.summary(pid).then(s => { if (store.epoch === mine) store.summary = s })
    .catch(e => { if (store.epoch === mine) store.summaryErr = e.message })
  api.touchPaper(pid).then(() => refreshPapers()).catch(() => {})
}

export async function refreshAnalysis() {
  if (!store.currentId) return
  const mine = store.epoch
  const a = await api.analysis(store.currentId)
  if (store.epoch !== mine) return             // 回来时已经换篇：这是上一篇的骨架，丢掉
  Object.assign(store.analysis, a)
  store.paper = await api.paper(store.currentId)   // 同步 abbrs 等字段
  refreshPapers()
}

export async function refreshMarginalia() {
  if (!store.currentId) return
  const mine = store.epoch
  const m = await api.marginalia(store.currentId)
  if (store.epoch !== mine) return             // 同上：别把上一篇的批注装到这篇上
  Object.assign(store.marginalia, m)
}

/* 重新析读会把一眼卡一并作废（它是旧主张的产物），所以析读完成后要重新取一次。
   取的过程本身会触发生成，页面上就是"正在写一眼卡…"再转一圈——这是对的，
   总比留一张对不上新主张的卡片好。 */
export async function reloadSummary() {
  if (!store.currentId) return
  store.summary = null
  store.summaryErr = ''
  try { store.summary = await api.summary(store.currentId) }
  catch (e) { store.summaryErr = e.message }
}

export function jumpTo(page, y0, y1) {
  store.jump = { page, y0, y1, at: Date.now() }
}
