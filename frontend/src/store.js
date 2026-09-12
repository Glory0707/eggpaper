import { reactive, watch, computed } from 'vue'
import { api } from './api'

/* 只转出组件真的会 import 的那些。颜色/标签的字典（KIND_*、BAND_*）不再对外——
   它们只该通过下面这三个解析口被读到，散出去就又会有人绕过口径直接取色。 */
export { api, askStream, ROLE_ZH, CORE_ROLES, ROLE_COLOR, ROLE_TEXT_COLOR,
         bandOf, kindColor, kindText, kindZH } from './api'

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
  marginalia: { status: 'none', notes: [], progress: null },
  summary: null,
  summaryErr: '',
  settings: null,
  // 文库：分类 + 搜索 + 排序。map 是 paper_id → [分类 id]，一次拉全，列表里不用逐篇问
  lib: { colls: [], map: {}, coll: 'all', q: '', sort: lsGet('libSort', 'added') },
  viewer: {
    variant: lsGet('variant', 'original'),
    spread: lsGet('spread', 'spread'),
    layers: lsGet('layers', { marginalia: true, skim: false }),
    care: lsGet('care', 'off'),            // 护眼底纹：off / mung / cyan / sand
    fs: lsGet('fs', 'std'),                // 字号：sm / std / lg / xl（论文正文不受影响）
    railUser: lsGet('railUser', true),     // 用户对右栏的偏好；双语对开姿势可临时覆盖
    railW: lsGet('railW', 336),            // 右栏宽度：可拖可双击复位
    frame: false,
    libOpen: false,
  },
  jump: null,            // {page, y0, y1, at}
  cite: { open: false }, // 「引用」浮层：开在顶栏标题旁，内容由 CiteCard 自己拉
  // 更新：打开软件时后台静默查一次，有新版才把 show 打开（UpdateCard 读这一份）
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
  viewerApi: null,       // PdfViewer 注册：{step, translateCurrent, jumpBack, translateSelectionKey}
  visPrefill: null,   // {img, question} 图表灯箱带过来的视觉问答

  // 窄窗（半屏、竖屏、小笔记本）：右栏不再占版面，改成浮在书桌上的抽屉
  get narrow() { return this.vw < 1180 },
  get railRight() {
    if (this.viewer.variant === 'dual' && this.viewer.spread === 'spread') return false
    return this.viewer.railUser
  },
})

window.addEventListener('resize', () => { store.vw = window.innerWidth })

export function toast(msg, ms = 2600) {
  store.toast = msg
  clearTimeout(toast._t)
  toast._t = setTimeout(() => (store.toast = ''), ms)
}

watch(() => store.viewer.variant, v => lsSet('variant', v))
watch(() => store.viewer.spread, v => lsSet('spread', v))
watch(() => store.viewer.layers, v => lsSet('layers', v), { deep: true })
watch(() => store.viewer.railUser, v => lsSet('railUser', v))
watch(() => store.viewer.railW, v => lsSet('railW', v))
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

// 旧版本在 <html> 上留过 data-skin（皮肤已删）：留着只会让 devtools 里多一个没用的属性
delete document.documentElement.dataset.skin
// 同理：layers 里留过 skeleton（页边角色书签已删），清掉免得它继续被存回去
delete store.viewer.layers.skeleton

/* 查更新。silent=true（默认）时一切失败都咽下去——这是打开软件时的一次安静探测，
   源没配、网断了、源上没东西，都不该变成一个报错弹窗。 */
export async function checkUpdate(force = false, silent = true) {
  try {
    const r = await api.updateCheck(force)
    if (r.has_update) store.update = { ...store.update, ...r, show: true }
    return r
  } catch (e) {
    if (!silent) toast('检查更新失败：' + e.message)
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

export async function openPaper(pid) {
  store.currentId = pid
  const pos = lsGet(`pos:${pid}`, {})
  if (pos.variant) store.viewer.variant = pos.variant
  if (pos.spread) store.viewer.spread = pos.spread
  store.paper = await api.paper(pid)
  // 换篇先清干净再装新的：上一章的划线和眉批在新论文上闪一下，比慢半拍难看得多
  // （症状：新论文的页面上短暂出现别人家的划线和批注卡）
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
  // 一眼卡是后台压的：压不出来（比如扫描件）要说出来，别让"正在写一眼卡…"一直转
  api.summary(pid).then(s => (store.summary = s)).catch(e => (store.summaryErr = e.message))
  api.touchPaper(pid).then(() => refreshPapers()).catch(() => {})
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
