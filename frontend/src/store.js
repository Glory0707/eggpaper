import { reactive, watch, computed } from 'vue'
import { api, kindZH } from './api'
import { t, ui } from './i18n'
import { lsGet, lsSet } from './ls'

/* 只转出组件真的会 import 的那些；页边卡的颜色/笔触统一在 styles.css，不经这里。 */
export { api, askStream, ROLE_ZH, ROLE_COLOR, ROLE_TEXT_COLOR, kindZH } from './api'
export { lsGet, lsSet, lsRemove } from './ls'


const _clamp = (v, lo, hi, d) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d)

export const store = reactive({
  vw: window.innerWidth,     // 视口宽度：窄窗要换一套排布（右栏改浮层、栏位让给论文）
  papers: [],
  currentId: null,
  paper: null,
  paras: [],
  analysis: emptyAnalysis(),
  marginalia: emptyMarginalia(),
  summary: null,
  summaryErr: '',
  settings: null,
  openIds: [],           // 多文献同屏的窗格（1=单篇；2/3 横排、4 四宫格）；currentId = 活动窗格那篇
  lib: { colls: [], map: {}, coll: 'all', sort: lsGet('libSort', 'added') },
  viewer: {
    variant: lsGet('variant', 'original'),
    spread: lsGet('spread', 'spread'),
    layers: { mine: true, ...lsGet('layers', {}) },
    care: lsGet('care', 'off'),            // 护眼底纹：off / mung / cyan / sand
    fs: lsGet('fs', 'std'),                // 字号：sm / std / lg / xl（论文正文不受影响）
    railUser: lsGet('railUser', true),     // 用户对右栏的偏好；双语对开姿势可临时覆盖
    /* 栏宽读入即钳位：localStorage 是能被旧版本/手动改坏的地方，99999px 一读进来
       布局就爆（拖动本身有 min/max 把关，唯独「读回」这条路没人管）。 */
    railW: _clamp(lsGet('railW', 336), 268, 760, 336),
    libW: _clamp(lsGet('libW', null) ?? 300, 236, 560, 300),
    frame: false,
    libOpen: false,
    calOpen: false,        // 论文日历抽屉（与文库同侧，互斥打开）
    tocOpen: false,        // 目录抽屉（同一侧第三层）
  },
  jump: null,            // {page, y0, y1, at}
  cite: { open: false }, // 「引用」浮层：开在顶栏标题旁，内容由 CiteCard 自己拉
  egg: { nod: 0 },       // 陪伴信号：每次向 AI 提问 +1，蛋歪头看你一眼（App 侧 watch）
  update: {
    show: false, current: '', latest: '', notes: '', url: '', size: 0, sha256: '',
    pub_date: '', required: false, packaged: false, installing: false,
    prog: { state: 'idle', pct: 0, got: 0, total: 0, path: '', error: '' },
  },
  askPrefill: null,      // {paraIdx} 或 {text}
  glossaryPrefill: null,
  shortcutCard: false,
  askFocusTick: 0,
  crossPaperBack: null,  // 跨篇跳转的迁出点（pid）：Alt+← 开回原篇；手动换篇即失效
  crossJumping: false,   // gotoPaperPara 的 openPaper 正在进行：这次的换篇不算手动
  escTick: 0,            // 按 Esc 递增：PDF 侧的浮层（划词/框选/查找）据此全部收起
  readingPara: null,     // 当前视口中心附近段落（scroll-spy）
  reflowTick: 0,          // 栏宽拖完递增一次：论文据此重新定标（拖的过程中不重排）
  toast: '',
  toastN: 0,             // 连发时递增：模板 :key 让 pop 动画每次重放
  epoch: 0,              // 换一篇 +1：按篇的异步请求回来时对不上就丢掉（见 openPaper）
  viewerApi: null,       // PdfViewer 注册：{step, stepPage, gotoPage, translateCurrent, translateSelectionKey, jumpBack, startShot, openSearch, findInPaper}
  visPrefill: null,   // {img, question} 图表灯箱带过来的视觉问答

  get narrow() { return this.vw < 1180 },
  /* 双语·对开：屏宽全给左右两页，右栏让位。railRight 与 railOverlay 都要判这一条，
     以前同一条式子抄两遍（改一处漏一处就会「有种姿势收不干净」）。 */
  get dualSpread() { return this.viewer.variant === 'dual' && this.viewer.spread === 'spread' },
  get railRight() {
    if (this.dualSpread) return false
    return this.viewer.railUser
  },
  /* 多窗格（对比读两篇）也要能提问/看七问/导出：右栏与单篇同口径——占位挤压窗格。
     2026-10-12 端到端走查改回挤压：浮层方案（盖在窗格上）会遮住最右窗格的
     标题栏关闭钮与页边批注（实测点击落到右栏自己的收起钮上），藏内容比压窄更糟。 */
})

window.addEventListener('resize', () => { store.vw = window.innerWidth })

export function toast(msg, ms = 2600) {
  store.toast = t(String(msg ?? '')).slice(0, 160)   // 后端偶发的长报错别把黑条撑满屏
  store.toastN++
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
watch(() => store.viewer.libW, v => lsSet('libW', v))
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

/* 文库版本号对账：每 5 秒问一次后端"篇数/最新导入变没变"，变了就静默刷新列表。
   没有它，开久了的窗口拿着陈旧列表去拖拽归类，就会撞「论文不存在」——
   那篇多半在别的窗口被删了，这个页面永远不知道。 */
let _libV = ''
let _libWatch = 0
export function startLibraryWatch() {
  if (_libWatch) return             // 重挂（HMR/多壳）不再叠一条 5 秒轮询：清单只会越跑越多
  _libWatch = setInterval(async () => {
    if (document.hidden) return       // 挂后台不刷；回来那一拍 v 对不上会自动补一次刷新
    try {
      const { v } = await api.libraryVersion()
      if (v && _libV && v !== _libV) await refreshPapers()
      _libV = v
    } catch { /* 服务退了：下一拍再说 */ }
  }, 5000)
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
   为什么要有它：一眼卡/七问/方法卡都是**秒级**的模型调用，用户"打开 A 看一眼
   就点 B"时，A 的答案会落在 B 上（B 的速览页显示 A 的发现、A 的角色套到 B 的段落上），
   而 store.openPaper 是手写清场的——漏一个字段就漏一个洞。 */
/* 按篇状态的两个空壳：初始值、换篇、出错三条路都要摆出同形的骨架。手写字面量
   迟早漏字段（漏了 evidence_qs 就是模板里一个 undefined 的小坑）。 */
export function emptyAnalysis(extra = {}) {
  return { status: 'none', claims: [], annotations: {}, evidence_qs: {}, error: '', ...extra }
}
export function emptyMarginalia() { return { notes: [] } }

/* 当前活动篇在 papers 里的那条记录（顶栏标题、译文/双语按钮的可用性都看它）。 */
export const currentPaper = computed(() => store.papers.find(p => p.id === store.currentId) || null)

/* 显示名：title 可能是空串（元数据没补齐），退回文件名。20 处调用点是同一个口径，
   各写一份 `p.title || p.filename` 时，加一层兜底（如「（无标题）」）就得挨个改。 */
export function paperName(p, fallback = '') {
  return ((p && (p.title || p.filename)) || '') || fallback
}

export function paperEpoch() { return store.epoch }
export function samePaper(mine) { return store.epoch === mine }

/* 回书桌：当前论文清场、文库不动。阅读位置本来就存在本地，回来随时接上。
   这也是「主页」的正式入口：字标点击 / g h / 启动时按上次的记录。 */
export function goHome() {
  lsSet('lastPaper', '')
  store.currentId = null
  store.openIds = []
  store.epoch++
  store.paper = null
  store.paras = []
  store.analysis = emptyAnalysis()
  store.marginalia = emptyMarginalia()
  store.readingPara = null
  store.summary = null
  store.summaryErr = ''
}

export async function openPaper(pid) {
  /* 多窗格模式下，文库/投递来的"打开这篇"= **替换活动窗格**（双开不打回单篇）；
     单篇时 = 整屏换成这一篇。 */
  if (store.openIds.length > 1 && store.openIds.includes(store.currentId)) {
    if (store.openIds.includes(pid)) {
      // 要打开的这篇已经在别的窗格里（比如点了一条跨篇引用，指向同屏的另一篇）：
      // 切过去就行。替换的话 openIds 会出现两个同 pid 的窗格——:key 撞车、
      // 同一章纸渲染两遍，后面对这个 pid 的批量操作还会一次命中两格
      if (store.currentId !== pid) await activatePaper(pid, false)
      return
    }
    store.openIds[store.openIds.indexOf(store.currentId)] = pid
    await activatePaper(pid, false)      // 全局 variant 保持：另一窗格的姿势不被打扰
    return
  }
  store.openIds = [pid]                  // 单篇打开：整个窗格组换成这一篇
  await activatePaper(pid, true)
}

/* 多文献同屏：一屏最多四篇（2/3 篇横排、4 篇四宫格），openIds 驱动布局。
   currentId = **活动窗格**那篇——右栏、顶栏、快捷键、析读翻译全跟着它走；
   点哪个窗格谁就是活动篇。variant 是全局的（所有窗格一起切原文/译文）。
   paneBusy：加/关/切是同一份全局数据的串行事务，并发调用只让最先到的做。 */
let paneBusy = false

export async function addPane(pid) {
  if (paneBusy) return { busy: true }
  if (store.openIds.includes(pid)) {
    if (store.currentId === pid) return {}          // 已是活动篇：什么都不用做
    paneBusy = true
    try { await activatePaper(pid, false) } finally { paneBusy = false }
    return { dup: true }
  }
  if (store.openIds.length >= 4) return { full: true }
  if (!store.openIds.length || !store.currentId) { await openPaper(pid); return {} }
  paneBusy = true
  try {
    store.openIds.push(pid)
    await activatePaper(pid, false)
  } finally { paneBusy = false }
  return {}
}

export async function closePane(i) {
  if (paneBusy) return
  if (i < 0 || i >= store.openIds.length) return    // 旧渲染帧的索引：防御
  paneBusy = true
  try {
    const pid = store.openIds[i]
    const wasActive = store.currentId === pid
    const next = store.openIds.filter((_, k) => k !== i)[0]
    if (wasActive && next) store.currentId = next   // 先切活动，避免中间帧渲染被关的那篇
    store.openIds.splice(i, 1)
    if (wasActive) {
      if (next) await activatePaper(next, false)
      else goHome()
    }
  } finally { paneBusy = false }
}

export async function activatePaper(pid, applyVariant = true) {
  if (store.crossJumping) store.crossJumping = false
  else store.crossPaperBack = null          // 手动换篇：跨篇返回点失效
  const mine = ++store.epoch
  lsSet('lastPaper', pid)
  store.currentId = pid
  const pos = lsGet(`pos:${pid}`, {})
  if (applyVariant) {
    if (pos.variant) store.viewer.variant = pos.variant
    if (pos.spread) store.viewer.spread = pos.spread
    if (ui.lang === 'en') store.viewer.variant = 'original'   // 英文模式没有译文/双语
  }
  store.paras = []
  store.analysis = emptyAnalysis()
  store.marginalia = { ...emptyMarginalia(), pid }
  store.readingPara = null
  store.summary = null
  store.summaryErr = ''
  try {
    const [paper, paras] = await Promise.all([api.paper(pid), api.paragraphs(pid)])
    if (store.epoch !== mine) return       // 已被更新的激活顶掉：这份是过站的，丢
    store.paper = paper
    store.paras = paras
    if (applyVariant && store.viewer.variant !== 'original' && paper.translate_status !== 'done') {
      store.viewer.variant = 'original'
    }
    refreshAnalysis()
    refreshMarginalia()
    api.summary(pid).then(s => { if (store.epoch === mine) store.summary = s })
      .catch(e => { if (store.epoch === mine) store.summaryErr = e.message })
    api.touchPaper(pid).then(() => refreshPapers()).catch(() => {})
  } catch (e) {
    if (store.epoch === mine) store.analysis = { status: 'error', error: e.message, claims: [], annotations: {}, evidence_qs: {} }
  }
}

store.activatePaper = activatePaper
store.addPane = addPane
store.closePane = closePane

/* 按篇取数的同构骨架：钉住取数那一刻的篇，回来时还是同一篇才落状态。
   骨架/批注/一眼卡三处本来是同一段手抄三遍——漏一次比对，上一篇的数据就会盖到
   这一篇上（这类竞态只在快速换篇时可见，最难查）。取数失败的处理交给调用方：
   骨架与批注是"篇没了就静下来"，一眼卡是"记下错误"。 */
async function fetchPaperData(fetchFn, apply, onErr) {
  if (!store.currentId) return
  const pid = store.currentId
  const mine = paperEpoch()
  let data
  try {
    data = await fetchFn(pid)
  } catch (e) {
    if (samePaper(mine)) onErr?.(e)
    return
  }
  if (!samePaper(mine)) return
  apply(data)
}

export async function refreshAnalysis() {
  // 取数失败（那一篇已经不在了，批量删除的竞态）就静下来，别冒 404 的 toast
  await fetchPaperData(api.analysis, async a => {
    Object.assign(store.analysis, a)
    try { store.paper = await api.paper(store.currentId) } catch { return }   // 同步 abbrs 等字段
    refreshPapers()
  })
}

export async function refreshMarginalia() {
  await fetchPaperData(api.marginalia, m => {
    Object.assign(store.marginalia, { notes: m.notes || [], pid: store.currentId })
  })
}

/* 重新析读会把一眼卡一并作废（它是旧主张的产物），所以析读完成后要重新取一次。
   取的过程本身会触发生成，页面上就是"正在写一眼卡…"再转一圈——这是对的，
   总比留一张对不上新主张的卡片好。 */
export async function reloadSummary() {
  // 生成隔着一次 LLM 调用，期间可能已换到别的论文：A 的摘要不能落到 B 的速览页上
  store.summary = null
  store.summaryErr = ''
  await fetchPaperData(api.summary, sum => { store.summary = sum },
                       e => { store.summaryErr = e.message })
}

/* 页边卡上的「问 ↗」：质疑这条批注的问题直接带进提问面板并发出去。
   原先在 RightRail（右栏的卡）和 PdfViewer（纸上的卡）各写一份，措辞一改就漏一边。 */
export function askNotePrefill(n, { openRail = false } = {}) {
  if (openRail) store.viewer.railUser = true
  const kind = kindZH(n) || t('批注')
  store.askPrefill = {
    question: t('页边批注「{kind}」：「{note}」，引文：“{quote}”。依据在哪？[¶{n}]',
                { kind, note: n.note, quote: (n.quote || '').slice(0, 60), n: n.para_idx }),
    send: true,
  }
}

export function jumpPara(idx) {
  const p = paraByIdx.value[idx]
  if (p) jumpTo(p.page, p.bbox.y0, p.bbox.y1)
}

/* 跨篇落段：异篇先 openPaper 再按 ¶n 落位（位置记忆管回来），同篇直接跳。
   三个组件（提问引用/对比表/互引列表）原本各写一份 openPaper().then()。
   迁出点记进 crossPaperBack：Alt+← 开回原篇（§4 原则 1「任何跳转入栈，回到迁出点」
   的跨篇版）。手动换篇即失效——只在跳转链上有效。 */
export function gotoPaperPara(pid, n) {
  if (!pid) return
  if (pid === store.currentId) { if (n) jumpPara(n); return }
  store.crossPaperBack = store.currentId || null
  store.crossJumping = true
  openPaper(pid).then(() => { if (n) jumpPara(n) })
}

export function jumpTo(page, y0, y1) {
  store.jump = { pid: store.currentId, page, y0, y1, at: Date.now() }
}
