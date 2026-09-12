<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, paraByIdx, CORE_ROLES, ROLE_ZH, bandOf, kindColor, kindText, kindZH } from '../store'
import { lineSpanOf, findQuoteRects, findAllRects, clearTextIndex, sentenceAround } from '../find'
import { prettyChem } from '../chem'
import { translateStream } from '../api'
import MdLite from './MdLite.vue'
import EggMark from './EggMark.vue'
import { vDrag } from '../drag'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl


const GUTTER_FULL = 184   // 页边批注带（184 宽 + 12 的左边距）
const LS_POS = 'eggpaper:pos:'

const deskEl = ref(null)
const ready = ref(false)
const loadPct = ref(0)         // 破壳进度：喂给入场那条细线
let creep = 0                   // 缓慢逼近的底速（本地 PDF 的下载是瞬时的，
                                // pdf.js 的进度回调基本不触发——只靠它，线会一直停在 0%）
const zoom = ref(1)              // 在"适宽/适页"之上的微调倍率
const fit = ref('width')         // width 适宽 / page 适页 / none 固定百分比
const fitScale = ref(1)          // 适宽比例：容器宽 ÷ 纸宽
const pageFitScale = ref(1)      // 适页比例：容器高 ÷ 纸高
const pageNum = ref(1)           // 当前页（1 起，跟着滚动更新）
const pageIn = ref('1')          // 页码输入框里的字
const searchOpen = ref(false)
const searchQ = ref('')
const searchHits = ref([])       // [{page, gi, y, rects}]
const searchAt = ref(-1)
const searchBusy = ref(false)
const midX = ref(0)              // 书桌中线：缩放条/提示贴它，而不是视口中线
const deskRightX = ref(0)        // 书桌右缘：阅读进度细线贴它
const sheets = ref([])
const noteHeights = ref({})      // 旁批实测高度，摊平用
const expandedNote = ref(null)   // 展开的批注卡
const pendingPara = ref(null)    // 段译进行中
const freshNotes = ref(false)
const backChip = ref(false)
const rendering = ref(false)     // 正在出图：顶部一条细线，不遮内容
const flash = ref(null)          // { idx, gi } 标出整段；或 { boxes, gi } 精确标出引文
const pageInputEl = ref(null)
const searchInputEl = ref(null)

const canvases = ref([]), textLayers = ref([]), pageEls = ref([])
const doneKeys = new Set()
const renderQueues = new Map()   // gi -> 渲染链尾：同一画布严格串行，杜绝并发 render
let passToken = 0
const scroller = () => deskEl.value?.closest('.desk') || deskEl.value
let docs = { orig: null, dual: null, mono: null }
let ro = null

const scale = computed(() => {
  const base = fit.value === 'page' ? Math.min(fitScale.value, pageFitScale.value)
             : fit.value === 'none' ? 1
             : fitScale.value
  return Math.min(3, Math.max(0.2, base * zoom.value))
})
const zoomPct = computed(() => Math.round(scale.value * 100))
const parasByPage = computed(() => {
  const m = {}
  for (const p of store.paras) (m[p.page] ||= []).push(p)
  return m
})
/* 页边摆哪些批注：图层开关 × 档位过滤。四档就是纸上那四种笔触，
   一档一个开关（右栏「问题」页的眉批块里点）——批注上到三四十条时，
   "只看要当心"是读者的第一个念头。 */
const notesShown = computed(() => {
  if (!store.viewer.layers.marginalia) return []
  const on = store.viewer.noteBands || {}
  return store.marginalia.notes.filter(n => on[bandOf(n)] !== false)
})
/* 页边要多宽，取决于"上面真有东西要放吗"：有批注 → 整条 184，没有 → 一点都不留，
   论文因此能多出近 200px 的宽度。这条宽度是 measure() 的输入，图层一变就得重排。 */
const gutterW = computed(() => (store.viewer.layers.marginalia && notesShown.value.length ? GUTTER_FULL : 0))
const gutterPad = computed(() => (gutterW.value ? 12 : 0))
const flatItems = computed(() => sheets.value.flatMap(s => s.items))

// 角色（8 类）现在只服务于一件事：略读时该把哪几段蒙掉。界面上不再有它的位置
function roleOf(p) {
  return store.analysis.annotations[String(p.idx)]?.role || null
}
function isCore(p) { return CORE_ROLES.includes(roleOf(p) || 'background') }

/* ---------------- 略读：判得准不准，读者要看得见、也要能自己扳 ----------------
   略读只做一件事：把"不用细读"的段落蒙掉（依据是析读判的角色）。但角色是**模型判的**，
   它会把一段重要的前人工作判成"背景铺垫"——读者在纸上明明看得见那句话要紧。所以三件事：
   ① 蒙纱与核心段都能看见"为什么"（纸边标角色名，只在这一层图层开着时出现）；
   ② 悬停把这一块**掀开**看一眼，点一下是"这段我也要读"（按篇记在本地，
      这属于读者对这篇的判断，不是全局偏好）；③ 顶部一行说清留了几段、略了几段、
      还有几段根本没判过角色——略读的"精确度"得让人能核对，否则只是个特效。 */
const LS_KEEP = 'eggpaper:skimKeep:'
function loadKeep() {
  try { return new Set(JSON.parse(localStorage.getItem(LS_KEEP + store.currentId) || '[]')) } catch { return new Set() }
}
const skimKeep = ref(loadKeep())
const pingId = ref(null)                  // 刚从纸上点回来的那条批注（亮一下）
const kept = idx => skimKeep.value.has(idx)
function toggleKeep(idx) {
  const s = new Set(skimKeep.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  skimKeep.value = s
  try { localStorage.setItem(LS_KEEP + store.currentId, JSON.stringify([...s])) } catch { /* 存不下就只管这一次会话 */ }
}
function clearKeep() {
  skimKeep.value = new Set()
  try { localStorage.removeItem(LS_KEEP + store.currentId) } catch { /* 同上 */ }
}
/* 有「值得读 / 要当心」批注的段落不蒙。模型自己在那一段插了句话，说明那儿有东西要看——
   把整段蒙掉等于把刚写下的提醒一起藏起来。略读该略的是没有信息量的铺垫，
   不是**有批注的段**。（"你写的"那两条不算：那是你自己划的，你记得住。） */
const protectedIdx = computed(() => {
  const s = new Set()
  for (const n of notesShown.value) {
    const b = bandOf(n)
    if (b === 'good' || b === 'warn') s.add(n.para_idx)
  }
  return s
})
// 此刻被蒙掉吗：略读开着 + 判过角色 + 不是核心 + 读者没手动留下 + 段上没有批注
function veiled(p, pno) {
  return store.viewer.layers.skim && pno >= 0 && !!roleOf(p) && !isCore(p)
    && !kept(p.idx) && !protectedIdx.value.has(p.idx)
}
const skimStats = computed(() => {
  let keepN = 0, skipN = 0, unknown = 0, mine = 0, byNote = 0
  for (const p of store.paras) {
    if (isCore(p)) { keepN++; continue }
    if (kept(p.idx)) { keepN++; mine++; continue }
    if (protectedIdx.value.has(p.idx)) { keepN++; byNote++; continue }
    if (!roleOf(p)) { unknown++; continue }
    skipN++
  }
  return { kept: keepN, skipped: skipN, unknown, mine, byNote }
})
// 纸边那行小字：说清"为什么留下了这一段"——角色名 / 你要读 / 有眉批
function roleTag(p) {
  if (kept(p.idx) && !isCore(p)) return '你要读'
  if (protectedIdx.value.has(p.idx) && !isCore(p)) return '有眉批'
  return ROLE_ZH[roleOf(p) || ''] || ''
}

/* ---------------- 文档装载与 sheets 构建 ---------------- */

async function getDoc(kind) {
  if (!docs[kind]) {
    const task = pdfjsLib.getDocument(`/api/papers/${store.currentId}/pdf?variant=${kind}`)
    // 首次打开才报进度：换姿势是本地重排，不需要（也不会有）下载进度
    if (!sheets.value.length) {
      task.onProgress = ({ loaded, total }) => {
        if (total) loadPct.value = Math.max(loadPct.value, Math.min(0.94, loaded / total))
      }
    }
    docs[kind] = await task.promise
  }
  return docs[kind]
}

async function meta(doc) {
  const vp = await (await doc.getPage(1)).getViewport({ scale: 1 })
  return { count: doc.numPages, w: vp.width, h: vp.height }
}

async function buildSheets() {
  const v = store.viewer.variant
  let g = 0
  const rows = []
  const push = (items) => { items.forEach(it => (it.gi = g++)); rows.push({ items }) }

  if (v === 'original') {
    const d = await getDoc('orig'), m = await meta(d)
    for (let i = 0; i < m.count; i++)
      push([{ key: `o${i}`, doc: 'orig', page: i, origPage: i, w: m.w, h: m.h, margin: true, text: true }])
  } else if (v === 'mono') {
    const d = await getDoc('mono'), m = await meta(d)
    for (let i = 0; i < m.count; i++)
      push([{ key: `m${i}`, doc: 'mono', page: i, origPage: -1, w: m.w, h: m.h, margin: false, text: false }])
  } else if (v === 'dual') {
    const td = await getDoc('dual'), tm = await meta(td)
    if (store.viewer.spread === 'spread') {
      const od = await getDoc('orig'), om = await meta(od)
      for (let i = 0; i < om.count && 2 * i + 1 < tm.count; i++)
        push([
          { key: `sl${i}`, doc: 'orig', page: i, origPage: i, w: om.w, h: om.h, margin: false, text: true },
          { key: `sr${i}`, doc: 'dual', page: 2 * i + 1, origPage: -1, w: tm.w, h: tm.h, margin: false, text: false },
        ])
    } else {
      for (let j = 0; j < tm.count; j++) {
        const isOrig = j % 2 === 0
        push([{ key: `di${j}`, doc: 'dual', page: j, origPage: isOrig ? j : -1, w: tm.w, h: tm.h, margin: isOrig, text: isOrig }])
      }
    }
  }
  sheets.value = rows
}

// 破壳那条线：真进度有就用真的，没有也让它一直往前挪一点（上限 90%），
// 免得"在等"和"卡死了"长得一样。到 1 由 ready 那一拍负责。
function startCreep() {
  stopCreep()
  creep = setInterval(() => {
    if (loadPct.value >= 0.9) return
    loadPct.value = Math.min(0.9, loadPct.value + (0.92 - loadPct.value) * 0.22)
  }, 240)
}
function stopCreep() { clearInterval(creep); creep = 0 }

async function load({ keepPlace = false } = {}) {
  loading = true
  const veryFirst = !sheets.value.length
  if (veryFirst) { ready.value = false; loadPct.value = 0.08; startCreep() }
  // 换姿势（原文/译文/双语/对开）前先记住读到哪里，换完再落回同一页同一高度
  const anchor = keepPlace || !veryFirst ? currentAnchor() : null
  sheets.value = []
  doneKeys.clear()
  try {
    await buildSheets()
  } catch (e) {
    // 译文/双语取不到（还没译、译文文件被删、翻译中途失败）：**退回原文**并说人话——
    // 停在空白纸面上时，用户只能自己猜到要回去点「原文」。
    if (store.viewer.variant !== 'original') {
      const was = store.viewer.variant
      store.viewer.variant = 'original'      // 赋值会触发 watch → 重新 load
      toast((was === 'mono' ? '译文版' : '双语版') + '打不开，已切回原文；想再看可重新「整本翻译」', 5000)
      loading = false
      return
    }
    toast('文档加载失败：' + e.message)
    ready.value = true
    loading = false
    return
  }
  await measure()
  await renderAll()
  stopCreep()
  loadPct.value = 1
  ready.value = true
  await nextTick()
  await measureNotes()
  if (anchor) applyAnchor(anchor)
  else if (store.viewer.restorePos) { scroller().scrollTop = store.viewer.restorePos; store.viewer.restorePos = 0 }
  updateProg()
  // 让 scroll-spy 先跑一拍：不进滚动也要把页码、"读至 ¶n"、进度线初始化好
  if (veryFirst) setTimeout(onScroll, 800)
  loading = false
}

/* 页码定位。三种模式的页号不是一回事：
   原文页 i / 译文第 i 页（1:1）/ 双语文档里 2i=原文、2i+1=译文。
   统一换算成"原文第几页"，跳转和位置记忆才不会跨模式错位。 */
function origPageOf(it) {
  if (!it) return 0
  if (it.origPage >= 0) return it.origPage
  if (it.doc === 'dual') return Math.floor(it.page / 2)
  return it.page
}
function pageItem(pno) {
  const items = flatItems.value
  return items.find(x => x.origPage === pno)
      || items.find(x => x.doc === 'mono' && x.page === pno)
      || items.find(x => x.doc === 'dual' && x.page === 2 * pno + 1)
      || null
}

/* 当前读到哪：原文页码 + 页内高度比例（比例让不同缩放/排布之间也能对上） */
function currentAnchor() {
  const sc = scroller()
  if (!sc) return null
  const items = flatItems.value
  if (!items.length) return null
  const top = sc.scrollTop + 8
  let cur = null
  for (const it of items) {
    const el = pageEls.value[it.gi]
    if (!el) continue
    if (el.offsetTop <= top) cur = it
    else break
  }
  if (!cur) return { page: origPageOf(items[0]), frac: 0 }
  const el = pageEls.value[cur.gi]
  const h = el?.offsetHeight || 1
  return {
    page: origPageOf(cur),
    frac: Math.min(1, Math.max(0, (top - (el?.offsetTop || 0)) / h)),
  }
}

function restoreAnchor(a, viewOff = 0) {
  const it = pageItem(a.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return false
  scroller().scrollTop = el.offsetTop + a.frac * (el.offsetHeight || 0) - viewOff
  return true
}

/* 落位要跟一堆异步赛跑（换模式渲染、右栏宽度动画、画布重定标），
   谁先谁后说不准，所以不赌一次成功：落完量一次，偏了就再落一次，
   最多四五拍收敛；用户一旦自己滚动就立刻撒手。 */
let anchorCancel = false
function applyAnchor(a, viewOff = 0) {
  if (!a || !scroller()) return
  anchorCancel = false
  let tries = 4
  const check = () => {
    if (anchorCancel || tries-- <= 0) return
    const it = pageItem(a.page)
    const el = it && pageEls.value[it.gi]
    if (!el) return
    const h = el.offsetHeight || 1
    const got = (scroller().scrollTop + viewOff - el.offsetTop) / h
    if (Math.abs(got - a.frac) <= 0.03) return          // 已经落对，收工
    restoreAnchor(a, viewOff)
    setTimeout(check, 110)
  }
  restoreAnchor(a, viewOff)
  setTimeout(check, 110)
}
function onUserScroll() { anchorCancel = true }

/* 视口里某个绝对高度落在"哪一页、页内几分之几" */
function anchorAt(absY) {
  const items = flatItems.value
  if (!items.length) return null
  let cur = null
  for (const it of items) {
    const el = pageEls.value[it.gi]
    if (!el) continue
    if (el.offsetTop <= absY) cur = it
    else break
  }
  if (!cur) return { page: origPageOf(items[0]), frac: 0 }
  const el = pageEls.value[cur.gi]
  return { page: origPageOf(cur),
           frac: Math.min(1, Math.max(0, (absY - el.offsetTop) / (el.offsetHeight || 1))) }
}

/* Ctrl+滚轮 = 缩放论文，不是浏览器缩放。这是读 PDF 的人肌肉记忆里的动作，
   也正好是"论文缩放"与"界面缩放"该分开的地方：Ctrl+± 交给浏览器缩整个界面，
   光标在纸上的 Ctrl+滚轮只缩这张纸。
   关键是锚点：光标底下那个字要留在原地，不能缩完就跑到别处去。 */
function onWheelZoom(e) {
  if (!e.ctrlKey && !e.metaKey) return
  const sc = scroller()
  if (!sc) return
  e.preventDefault()
  const viewOff = e.clientY - sc.getBoundingClientRect().top
  const a = anchorAt(sc.scrollTop + viewOff)
  stepZoom(e.deltaY < 0 ? 1 : -1)
  applyAnchor(a, viewOff)
}

/* ---------------- 渲染 ---------------- */

function measure() {
  const first = sheets.value[0]?.items?.[0]
  updateMid()
  const sc = scroller()
  if (!sc || !first) return
  const perRow = sheets.value[0].items.length
  const gutter = gutterW.value + gutterPad.value
  // 量的是滚动容器（书桌）的宽度，不是 .desk-inner——后者是 max-content，
  // 会随排版自己长大，拿它算"适宽"就成了正反馈：纸越大 → 容器越宽 → 纸更大。
  fitScale.value = (sc.clientWidth - 60 - gutter - (perRow === 2 ? 20 : 0)) / (first.w * perRow)
  // 适页：把一整页塞进书桌高度（留出上下内边距）
  pageFitScale.value = (sc.clientHeight - 56) / first.h
}

function updateMid() {
  const el = deskEl.value?.closest('.desk') || deskEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  midX.value = Math.round(r.left + r.width / 2)
  deskRightX.value = Math.round(r.right - 3)
}

// 容器宽度变了要重新定标：缩完要落回同一处，别让读者的视线跳走。
// 注意锚点只在一次连发里记第一拍——右栏折叠是 320ms 的动画，中途每帧都重记的话
// 记到的是"新宽度 + 旧页高"的错位坐标，落位就会偏。
let reflowT = null, reflowAnchor = null, loading = false
function reflow() {
  if (loading) return                       // 换模式那次由 load() 负责落位，别抢
  if (reflowT == null) reflowAnchor = currentAnchor()
  clearTimeout(reflowT)
  reflowT = setTimeout(async () => {
    reflowT = null
    measure()
    await renderAll()
    await measureNotes()
    if (reflowAnchor) applyAnchor(reflowAnchor)
    reflowAnchor = null
  }, 200)
}

async function renderAll() {
  const seq = ++passToken
  rendering.value = true
  await nextTick()
  for (let i = 0; i < flatItems.value.length; i++) {
    if (seq !== passToken) return
    await renderItem(flatItems.value[i])
  }
  if (seq === passToken) rendering.value = false
}

// 同一画布的渲染任务串成一条链，后来者排队；轮到执行时重新对齐当前
// scale / 当前 DOM——排队期间换过页或换过模式的活动直接作废。
function doRenderItem(it) {
  const key = it.key + ':' + scale.value.toFixed(3)
  if (doneKeys.has(key)) return
  if (!flatItems.value.includes(it)) return          // 这一项已被新布局替换
  const canvas = canvases.value[it.gi], tlEl = textLayers.value[it.gi], el = pageEls.value[it.gi]
  if (!canvas || !el) return
  const doc = getDoc(it.doc)
  return doc.then(async d => {
    const page = await d.getPage(it.page + 1)
    const viewport = page.getViewport({ scale: scale.value })
    const dpr = Math.min(2.5, window.devicePixelRatio || 1)
    canvas.width = Math.floor(viewport.width * dpr)
    canvas.height = Math.floor(viewport.height * dpr)
    canvas.style.width = viewport.width + 'px'
    canvas.style.height = viewport.height + 'px'
    el.style.setProperty('--scale-factor', scale.value)
    const ctx = canvas.getContext('2d', { alpha: false })
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    try {
      await page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null }).promise
      if (tlEl && it.text) {
        tlEl.innerHTML = ''
        clearTextIndex(tlEl)          // 节点全换了，旧的引文索引作废
        const tl = new pdfjsLib.TextLayer({ textContentSource: page.streamTextContent(), container: tlEl, viewport })
        await tl.render()
      }
      doneKeys.add(key)
    } catch (err) {
      console.error('[eggpaper] render fail', it.key, err)
    }
  })
}

function renderItem(it) {
  const prev = renderQueues.get(it.gi) || Promise.resolve()
  const task = prev.then(() => doRenderItem(it))
  renderQueues.set(it.gi, task.catch(() => {}))
  return task
}

function scheduleRender() {
  clearTimeout(scheduleRender._t)
  scheduleRender._t = setTimeout(renderAll, 160)
}

/* ---------------- 标注几何 ---------------- */

function rectStyle(p) {
  const b = p.bbox
  return { left: b.x0 * scale.value + 'px', top: b.y0 * scale.value + 'px',
           width: (b.x1 - b.x0) * scale.value + 'px', height: (b.y1 - b.y0) * scale.value + 'px' }
}

// 旁批高度靠实测：先按估算摆一遍，渲染后量真实高度再摆第二遍。
// 这样长批注展开后只会把下面的推开，不会压在别人身上。
function noteHeight(n) {
  const noteLines = Math.max(1, Math.ceil((n.note || '').length / 11))
  return 34 + Math.min(noteLines, 3) * 19 + 20
}

async function measureNotes() {
  await nextTick()
  computeQuoteMarks()
  const next = { ...noteHeights.value }
  let changed = false
  for (const el of document.querySelectorAll('.mg-note[data-nid]')) {
    const h = el.offsetHeight
    const id = el.dataset.nid
    if (!h) continue                       // 还没上桌，别拿 0 去摊平
    if (Math.abs((next[id] || 0) - h) > 0.5) { next[id] = h; changed = true }
  }
  if (changed) noteHeights.value = next
}

// 旁批是「钉在纸边的」，不是「长在纸里的」：一条展开变长了，整条页边就往下让，
// 让出来的高度只影响这一行的排布，绝不压到下一页身上。
// 高度靠实测收敛：先按估算摆一遍 → 渲染后量真实高度 → 再摆一遍。
const pageLayouts = computed(() => {
  const out = {}
  for (const it of flatItems.value) {
    if (it.origPage == null || it.origPage < 0) continue
    const baseH = it.h * scale.value
    const cands = notesShown.value
      .filter(n => n.page === it.origPage)
      .map(n => ({ n, anchor: quoteY(n) }))
    if (pendingPara.value != null) {
      const p = paraByIdx.value[pendingPara.value]
      if (p && p.page === it.origPage)
        cands.push({ n: { id: 'pending', kind: 'lookup', page: it.origPage, quote: (p.text || '').slice(0, 60), note: '翻译中…' }, anchor: p.bbox.y0 * scale.value, pending: true })
    }
    cands.sort((a, b) => a.anchor - b.anchor)
    const notes = []
    let prevBottom = -1, bottom = 0
    for (const c of cands) {
      const top = Math.max(c.anchor, prevBottom + 8)
      prevBottom = top + (noteHeights.value[c.n.id] || noteHeight(c.n))
      bottom = prevBottom
      notes.push({ n: c.n, top, pending: !!c.pending })
    }
    out[it.origPage] = { notes, height: Math.max(baseH, bottom + 14) }
  }
  return out
})

function notesOnPage(pno) { return pageLayouts.value[pno]?.notes || [] }

/* ---------------- 引文落位：划线精确到行 ---------------- */

// 一条引文落到哪几行——用段落自带的行级坐标算，不依赖渲染，页边排序和跳转都用它
// （缓存放组件里，不往 store 的批注对象上挂字段：那是数据，别被排布逻辑污染）
const spanCache = new Map()
/* 卡片上显示的、纸上划的，必须是**同一句话**：模型引的半句先用段落原文补成整句
   （sentenceAround），补不出来才退回原引文。 */
const anchorCache = new Map()
function anchorText(n) {
  if (!n) return ''
  const k = n.id + '|' + n.para_idx
  if (!anchorCache.has(k)) {
    const p = paraByIdx.value[n.para_idx]
    anchorCache.set(k, (p && n.quote && sentenceAround(p.text, n.quote)) || n.quote || '')
  }
  return anchorCache.get(k)
}
function quoteSpan(n) {
  if (!n) return null
  const k = n.id + '|' + n.para_idx
  if (!spanCache.has(k)) {
    const p = paraByIdx.value[n.para_idx]
    const t = anchorText(n)
    spanCache.set(k, t && p ? lineSpanOf(p, t) : null)
  }
  return spanCache.get(k)
}
// 框选钉子和引文钉子是两回事：前者锚在用户圈的那块矩形上（rect 就是唯一真相），
// 后者要在原文里重新找引文。旧数据只有靠 quote 的前缀分辨，新数据看 kind。
function isRegion(n) { return n.kind === 'region' || (n.quote || '').startsWith('[选区]') }
function regionBox(n) {
  const s = scale.value
  return n.rect ? [{ x: n.rect.x0 * s, y: n.rect.y0 * s, w: (n.rect.x1 - n.rect.x0) * s, h: (n.rect.y1 - n.rect.y0) * s }] : []
}

// 纸面上那段引文的精确矩形（逐行，DOM 量出来的），只给当前已渲染的页算
const quoteMarks = ref({})          // noteId -> [{x,y,w,h}]
const quoteCov = ref({})            // noteId -> 0~1：引文有多少能对回原文
function computeQuoteMarks() {
  const out = {}, cov = {}
  for (const n of notesShown.value) {
    if (isRegion(n) || !n.quote) continue
    const it = pageItem(n.page)
    const el = it && pageEls.value[it.gi]
    if (!el) continue
    // 把这一段在纸上的纵向范围也交给它：同一句话在页面上出现两次时，挑落在这一段里的那次
    const p = paraByIdx.value[n.para_idx]
    const box = p ? { y0: p.bbox.y0 * scale.value, y1: p.bbox.y1 * scale.value } : null
    const r = findQuoteRects(el, anchorText(n), box)
    if (r) { out[n.id] = r.rects; cov[n.id] = r.覆盖比 }
  }
  quoteMarks.value = out
  quoteCov.value = cov
}
// 引文对不上原文（模型改写、PDF 断词、跨栏）：划线只盖对得上的那截，卡片上要说一句
function quoteLoose(n) {
  const c = quoteCov.value[n.id]
  return c != null && c < 0.75
}

// 卡片和纸上那条线是一条命：鼠标停在卡片上，对应的划线跟着亮起来
const hotNote = ref(null)
function hoverNote(id) { hotNote.value = id }

/* 批注的标签：九种常用款用它自己的名字；模型自造的类型（kind='custom'）用模型起的标签；
   你自己钉的那些前面加"你 · "——一眼分得清哪句是别人说的、哪句是你自己写的。 */
const MINE = new Set(['lookup', 'region', 'note'])
function kindLabel(n) {
  const zh = kindZH(n)
  return MINE.has(n.kind) && zh ? '你 · ' + zh : zh
}

/* 引文默认只看开头，想看全句点「全句」。
   不硬切：切口带省略号，而且有明确的展开出口——页边只有 154px 宽，
   一条 200 字的引文全铺出来会把整页的批注挤下去。 */
const openQuote = ref(null)
// 摊开全句：卡片会变高，必须**跟着重新排版**（页边按"上一条下沿 + 8px"往下摆，
// 不重量一次，下面的卡不会让位、展开的引文会被盖住）。量两次：DOM 更新后 + 折行落定后。
function toggleQuote(n) {
  openQuote.value = openQuote.value === n.id ? null : n.id
  measureNotes()
  setTimeout(measureNotes, 280)
}
function quoteShown(n) {
  const q = anchorText(n)
  return openQuote.value === n.id || q.length <= 44 ? q : q.slice(0, 44) + '…'
}
// 没有 DOM 时的退路：按行级坐标画整行框（行数准，行内不裁）
function spanBoxes(n) {
  const span = quoteSpan(n)
  if (!span) return null
  return span.lines.map(l => ({
    x: l.bbox.x0 * scale.value, y: l.bbox.y0 * scale.value,
    w: (l.bbox.x1 - l.bbox.x0) * scale.value, h: (l.bbox.y1 - l.bbox.y0) * scale.value,
  }))
}
function markBoxes(n) {
  if (isRegion(n)) return regionBox(n)
  return quoteMarks.value[n.id] || spanBoxes(n) || []
}
// 引文的第一个字在纸上的 y（用来排序/跳转），拿不到就退回段落首行
function quoteY(n) {
  if (isRegion(n)) return (n.rect?.y0 || 0) * scale.value
  const m = quoteMarks.value[n.id]
  if (m?.length) return m[0].y
  const span = quoteSpan(n)
  if (span) return span.bbox.y0 * scale.value
  return (paraByIdx.value[n.para_idx]?.bbox.y0 || 0) * scale.value
}
const currentHit = computed(() => searchHits.value[searchAt.value] || null)
function searchHitsOnPage(pno) { return searchHits.value.filter(h => h.page === pno) }

// 阅读进度：一根贴书桌右缘的细线，读到哪长到哪。
// 用 ref 在 onScroll 里更新，不用 computed——computed 的依赖里没有"滚动位置"，
// 它只会在别的东西变化时重算，等于永远停在 0%。
const progPct = ref(0)
function updateProg() {
  const sc = scroller()
  if (!sc) return
  const total = sc.scrollHeight - sc.clientHeight
  progPct.value = total > 40 ? Math.min(1, Math.max(0, sc.scrollTop / total)) : 0
}

function toggleNote(n) {
  if ((n.note || '').length <= 34) return
  expandedNote.value = expandedNote.value === n.id ? null : n.id
  measureNotes()
}

async function translateParaAndPin(idx) {
  if (pendingPara.value != null) return
  pendingPara.value = idx
  // 流式攒出来的译文；流结束后一次性钉到页边（页边卡不逐字跳，钉的是成品）
  let zh = ''
  try {
    await translateStream(store.currentId, 'para', { idx }, ev => {
      if (ev.type === 'delta') zh += ev.text
      else if (ev.type === 'error') throw new Error(ev.message)
    }).done
    if (!zh.trim()) { toast('模型没返回内容，再试一次'); return }
    const p = paraByIdx.value[idx]
    await api.pin(store.currentId, { quote: (p?.text || '').slice(0, 150), note: zh, para_idx: idx, page: p?.page ?? 0 })
    await refreshM()
    toast('译文已钉在页边')
  } catch (e) { toast('翻译失败：' + e.message) }
  finally { pendingPara.value = null }
}

/* ---------------- 划词 ---------------- */

const sel = reactive({ visible: false, x: 0, y: 0, text: '', context: '', paraIdx: 0, page: 0, zh: '', hits: [], busy: false, err: '' })

function onMouseUp(e) {
  const s = window.getSelection()
  if (!s || s.isCollapsed || !s.rangeCount) { onPaperClick(e); return }
  const text = s.toString().trim()
  if (text.length < 2 || text.length > 3000 || !s.anchorNode) { sel.visible = false; return }
  const node = s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement
  if (!node?.closest?.('.textLayer')) { sel.visible = false; return }
  const range = s.getRangeAt(0)
  const r = range.getBoundingClientRect()
  const pageEl = node.closest('.page')
  const it = flatItems.value.find(x => pageEls.value[x.gi] === pageEl)
  // 定位不到段落时不能默认成 ¶0：那会把笔记钉到第一页的页边去。
  // 用选中文字所在页兜底，para_idx 记 -1（不参与同段重钉去重）
  let context = '', paraIdx = -1, page = it?.origPage ?? 0
  if (it && it.origPage >= 0) {
    const localY = r.top - pageEl.getBoundingClientRect().top
    for (const p of parasByPage.value[it.origPage] || []) {
      if (localY >= p.bbox.y0 * scale.value - 4 && localY <= p.bbox.y1 * scale.value + 4) {
        context = p.text; paraIdx = p.idx; page = p.page; break
      }
    }
  }
  Object.assign(sel, { visible: true, x: Math.min(window.innerWidth - 360, r.right + 10),
                       y: Math.min(window.innerHeight - 230, r.top),
                       text, context, paraIdx, page, zh: '', hits: [], busy: false, err: '' })
  selStream?.abort()          // 上一次的流别再往新气泡里写字
}

/* 纸上那条划线点一下要有回应。为什么不直接在 .mg-mark 上挂 click：那些块盖在正文上，
   一旦吃鼠标事件就没法选字了（见 styles.css 里那段注释）。所以走"整页 mouseup + 命中测试"——
   点一下（没拖动）本来也会触发 mouseup，选字、划词一条都不受影响。 */
function onPaperClick(e) {
  if (!store.viewer.layers.marginalia || !notesShown.value.length) return
  const pageEl = e.target?.closest?.('.page')
  if (!pageEl) return
  const it = flatItems.value.find(x => pageEls.value[x.gi] === pageEl)
  if (!it || it.origPage < 0) return
  const r = pageEl.getBoundingClientRect()
  const x = e.clientX - r.left, y = e.clientY - r.top
  for (const { n } of notesOnPage(it.origPage)) {
    if (markBoxes(n).some(b => x >= b.x - 2 && x <= b.x + b.w + 2 && y >= b.y - 2 && y <= b.y + b.h + 2)) {
      focusNote(n)
      return
    }
  }
}
/* 把对应的批注卡亮起来。**不滚动整页**——卡片就贴在这一行的页边，按"点一下纸"
   把论文滚走反而把人甩出原地；只有卡片真的在视野外（长批注把后面的卡推下去了）才最小幅度地滚。 */
function focusNote(n) {
  hotNote.value = n.id
  if ((n.note || '').length > 34) expandedNote.value = n.id
  pingId.value = n.id
  clearTimeout(focusNote._t)
  focusNote._t = setTimeout(() => { pingId.value = null; hotNote.value = null }, 1500)
  nextTick(async () => {
    await measureNotes()
    const el = document.querySelector(`.mg-note[data-nid="${n.id}"]`)
    const box = scroller()?.getBoundingClientRect()
    if (!el || !box) return
    const r = el.getBoundingClientRect()
    if (r.top >= box.top + 6 && r.bottom <= box.bottom - 6) return    // 已经在视野里：不滚
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  })
}
// 就这条批注追问模型：把批注与它引的原话一起交过去（从页边卡直接问，不必绕去右栏）
function askNote(n) {
  const kind = kindZH(n) || '批注'
  store.viewer.railUser = true
  store.askPrefill = {
    question: `眉批标了「${kind}」：「${n.note}」——引文是“${(n.quote || '').slice(0, 60)}”。`
      + `这条判断站得住吗？依据在哪几段？[¶${n.para_idx}]`,
    send: true,
  }
}

let selStream = null       // 进行中的划词翻译：换选区/关气泡时要掐掉，别让它往已关的气泡里写字
function closeSel() {
  selStream?.abort()
  sel.visible = false
  mine.open = false        // 手写批注的草稿框跟着气泡一起收，下次划词不该还开着
}

async function doTranslateSel() {
  selStream?.abort()
  sel.busy = true; sel.err = ''; sel.zh = ''; sel.hits = []
  try {
    await translateStream(store.currentId, 'selection', { text: sel.text, context: sel.context }, ev => {
      if (ev.type === 'delta') sel.zh += ev.text          // 字一到就显示，不等整段
      else if (ev.type === 'done') sel.hits = ev.hits || []
      else if (ev.type === 'error') { sel.err = ev.message; if (!sel.zh) sel.zh = '⚠ ' + ev.message }
    }).done
  } catch (e) {
    if (e.name !== 'AbortError') { sel.err = e.message; if (!sel.zh) sel.zh = '⚠ ' + e.message }
  }
  sel.busy = false
}

async function pinSel() {
  if (!sel.zh || sel.busy) await doTranslateSel()   // 流式版：busy 时也会把流等完
  if (!sel.zh) return
  await api.pin(store.currentId, { quote: sel.text.slice(0, 150), note: sel.zh, para_idx: sel.paraIdx, page: sel.page })
  await refreshM()
  closeSel()
  toast('已钉在页边')
}

// 译文的出口多一个：框选提问/钉页边之外，最常见的动作其实是"把这段译文贴到别处去"
async function copySel() {
  try {
    await navigator.clipboard.writeText(sel.zh || '')
    toast('译文已复制')
  } catch { toast('复制失败，手动选吧') }
}
function sendToGlossary() {
  store.glossaryPrefill = { term_en: sel.text.slice(0, 80), term_zh: (sel.zh || '').replace('〔演示译文〕', '').slice(0, 24) }
  closeSel()
  toast('已带到术语表，请确认中文译法')
  window.dispatchEvent(new CustomEvent('eggpaper:terms-prefill'))
}

function askAboutSel() {
  store.askPrefill = { text: sel.text.slice(0, 80) }
  closeSel()
}

/* 自己写一条批注：页边不只是"看批注的地方"，也是读者自己的本子。
   查译/段译是"AI 给你的"，这条是你自己的话——所以 kind='note'，颜色归"你自己的"那一档。 */
const mine = reactive({ open: false, text: '' })
const mineEl = ref(null)
function openMine() {
  mine.open = true
  mine.text = ''
  // 划完词接着就能写：不聚焦的话，用户得先在气泡里点一下输入框才打得出字
  nextTick(() => mineEl.value?.focus({ preventScroll: true }))
}
async function saveMine() {
  const t = mine.text.trim()
  if (!t) return
  await api.pin(store.currentId, { quote: sel.text.slice(0, 150), note: t, para_idx: sel.paraIdx,
                                   page: sel.page, kind: 'note' })
  mine.open = false; mine.text = ''
  await refreshM()
  closeSel()
  toast('已写在页边')
}

async function refreshM() {
  const m = await api.marginalia(store.currentId)
  Object.assign(store.marginalia, m)
}

async function unpin(mid) {
  await api.unpin(store.currentId, mid)
  await refreshM()
}

/* ---------------- 阅读器基本功能：缩放 / 页码 / 搜索 ---------------- */

function setFit(m) { fit.value = m; zoom.value = 1; saveLater() }
function stepZoom(d) {
  // 在"适宽/适页"之上微调；已经是固定比例就在当前比例上乘
  const next = scale.value * (d > 0 ? 1.15 : 1 / 1.15)
  fit.value = 'none'
  zoom.value = Math.min(3, Math.max(0.2, next))
  saveLater()
}
function gotoPage(p) {
  const pno = Math.min(store.paper?.n_pages || 1, Math.max(1, Math.round(p)))
  pageIn.value = String(pno)
  const it = pageItem(pno - 1)
  const el = it && pageEls.value[it.gi]
  if (!el) return
  backStack.push(scroller().scrollTop)
  scrollToY(el.offsetTop - 8, true)
  pageNum.value = pno
  backChip.value = true
  clearTimeout(applyJump._t)
  applyJump._t = setTimeout(() => (backChip.value = false), 5000)
}
function stepPage(d) { gotoPage(pageNum.value + d) }

// 近处滑过去、远处直接切：跨好几页的"平滑"滚动要滚一两秒，翻页就成了等动画。
// 翻页/跳页一律即时；同页内的微调（查找命中）才滑。
function scrollToY(y, instant = false) {
  const sc = scroller()
  if (!sc) return
  const far = Math.abs(y - sc.scrollTop) > sc.clientHeight * 1.5
  sc.scrollTo({ top: y, behavior: instant || far ? 'auto' : 'smooth' })
}

// 文档内搜索：在已渲染的文本层里找，命中位置用和引文划线同一套办法量，
// 所以跳过去落在哪、亮多长，都跟原文对得上。
async function runSearch() {
  const q = searchQ.value.trim()
  if (q.length < 2) { searchHits.value = []; searchAt.value = -1; return }
  searchBusy.value = true
  const hits = []
  for (const it of flatItems.value) {
    if (it.origPage < 0) continue
    const el = pageEls.value[it.gi]
    if (!el?.querySelector('.textLayer')) continue
    for (const h of findAllRects(el, q)) hits.push({ ...h, page: it.origPage, gi: it.gi })
  }
  hits.sort((a, b) => (a.page - b.page) || (a.y - b.y))
  searchHits.value = hits
  searchAt.value = hits.length ? 0 : -1
  searchBusy.value = false
  if (hits.length) gotoHit(0)
  else toast('没找到「' + q + '」')
}
function gotoHit(i) {
  const hits = searchHits.value
  if (!hits.length) return
  searchAt.value = (i + hits.length) % hits.length
  const h = hits[searchAt.value]
  const it = pageItem(h.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return
  // 查找命中一律即时落位：平滑滚动在长距离上要滚一两秒，而且落位不准就没法核对了
  scrollToY(el.offsetTop + h.rects[0].y - scroller().clientHeight * 0.3, true)
}
// 命中的高亮：给当前搜到的那条一个更大的底
function searchStep(d) { if (searchHits.value.length) gotoHit(searchAt.value + d) }
function closeSearch() { searchOpen.value = false; searchQ.value = ''; searchHits.value = []; searchAt.value = -1 }

function translateSelectionKey() {
  const s = window.getSelection()
  if (s && !s.isCollapsed && s.toString().trim()) onMouseUp({})
  else toast('先划选一段原文')
}

/* ---------------- 跳转栈 / 键盘 API ---------------- */

const backStack = []

async function applyJump() {
  const j = store.jump
  if (!j || !deskEl.value) return
  backStack.push(scroller().scrollTop)
  if (backStack.length > 30) backStack.shift()
  await nextTick()
  const it = pageItem(j.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return
  // 落点就是引文首行：跳过去的第一眼应该正好看见被引用的那句话
  scrollToY(el.offsetTop + j.y0 * scale.value - scroller().clientHeight * 0.28)
  flash.value = null
  if (j.rects?.length) {
    flash.value = { gi: it.gi, boxes: j.rects }
  } else {
    // 只给了 y：标出所在的那一段（用行级坐标兜底，别整段乱涂的时候多）
    for (const p of parasByPage.value[j.page] || []) {
      const y0 = (p.lines?.[0]?.bbox.y0 ?? p.bbox.y0) * scale.value
      const y1 = (p.lines?.[p.lines.length - 1]?.bbox.y1 ?? p.bbox.y1) * scale.value
      if (y0 <= j.y0 * scale.value + 6 && y1 >= j.y0 * scale.value - 6) { flash.value = { idx: p.idx, gi: it.gi }; break }
    }
  }
  setTimeout(() => (flash.value = null), 2400)     // 2400 > 入场 180 + 停留 1600 + 淡出 520
  backChip.value = true
  clearTimeout(applyJump._t)
  applyJump._t = setTimeout(() => (backChip.value = false), 5000)
}

/* 眉批卡片上的引文：点一下跳到纸上那句话，位置就是划线的位置 */
function jumpQuote(n) {
  const boxes = markBoxes(n)
  const it = pageItem(n.page)
  if (!it) return
  const y = boxes.length ? boxes[0].y / scale.value : (n.rect?.y0 ?? paraByIdx.value[n.para_idx]?.bbox.y0 ?? 0)
  store.jump = { page: n.page, y0: y, y1: y + 1, rects: boxes, at: Date.now() }
}

function jumpBack() {
  const t = backStack.pop()
  if (t == null) return
  scroller().scrollTo({ top: t, behavior: 'smooth' })
  backChip.value = false
}

function paraAbsY(idx) {
  const p = paraByIdx.value[idx]
  if (!p) return null
  const it = pageItem(p.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return null
  return el.offsetTop + p.bbox.y0 * scale.value
}

function step(dir) {
  if (!store.paras.length) return
  const skim = store.viewer.layers.skim
  let list = store.paras.filter(p => !skim || isCore(p))
  if (!list.length) list = store.paras
  const cur = store.readingPara
  let target
  if (cur == null) target = dir > 0 ? list[0] : list[list.length - 1]
  else {
    const at = list.findIndex(p => p.idx === cur)
    target = at < 0 ? list[0] : list[Math.min(list.length - 1, Math.max(0, at + dir))]
  }
  if (!target) return
  const y = paraAbsY(target.idx)
  if (y == null) return
  backStack.push(scroller().scrollTop)
  scroller().scrollTo({ top: y - scroller().clientHeight * 0.3, behavior: 'smooth' })
}

async function translateCurrent() {
  if (store.readingPara) await translateParaAndPin(store.readingPara)
  else toast('先滚动到要译的段落')
}

store.viewerApi = { step, translateCurrent, jumpBack, translateSelectionKey, stepPage, gotoPage, openSearch, findInPaper }

function openSearch() { searchOpen.value = true }
/* 别的面板（术语表）说"去原文里找这个词"：预填 + 打开 + 自动搜（searchQ 的 watch 会跑）。
   术语和原文本来是两个各自翻的地方，连起来之后"这个词在这篇里怎么用的"才问得出口。 */
function findInPaper(q) { searchQ.value = q; searchOpen.value = true }

/* ---------------- 滚动：scroll-spy + 位置记忆 ---------------- */

let spyT = null, saveT = null
function onScroll() {
  updateProg()
  const sc = scroller()
  const top = sc.scrollTop + 8
  let cur = 1
  for (const it of flatItems.value) {
    const el = pageEls.value[it.gi]
    if (el && el.offsetTop <= top) cur = origPageOf(it) + 1
    else if (el) break
  }
  if (cur !== pageNum.value) {
    pageNum.value = cur
    if (document.activeElement !== pageInputEl.value) pageIn.value = String(cur)
  }
  clearTimeout(spyT)
  spyT = setTimeout(() => {
    const focusY = sc.scrollTop + sc.clientHeight * 0.4
    let best = null, bestD = 1e9
    for (const p of store.paras) {
      const y = paraAbsY(p.idx)
      if (y == null) continue
      const d = Math.abs(y - focusY)
      if (d < bestD) { bestD = d; best = p.idx }
    }
    store.readingPara = best
    clearTimeout(saveT)
    saveT = setTimeout(savePos, 600)
  }, 220)
}

function savePos() {
  if (!store.currentId || !scroller()) return
  localStorage.setItem(LS_POS + store.currentId, JSON.stringify({
    scroll: scroller().scrollTop, variant: store.viewer.variant,
    spread: store.viewer.spread, zoom: zoom.value, fit: fit.value,
  }))
}
// 换了姿势也要记住：不能只在滚动时才存
function saveLater() { clearTimeout(saveT); saveT = setTimeout(savePos, 250) }

onMounted(async () => {
  // 打开一篇已经有眉批的论文：让划线和批注卡自己"画"进来一次，像有人刚在纸上划过。
  // （新生成的眉批走 store.marginalia.notes 的 watch，这里是"早就存在"的那种。）
  if (store.marginalia.notes.length) {
    freshNotes.value = true
    setTimeout(() => (freshNotes.value = false), 1800)
  }
  // 缩放/适页要在 load() **之前**定下来：load() 里会用 restorePos 写 scrollTop，
  // 而那个位置是按**当时那套缩放**算出来的像素。先加载再改适页，页高整个变了——存下来的
  // 位置就落到别处（用适页读过时缩放常常只有适宽的一半，跳到文末都算常事），而且
  // watch(scale) 会在 250ms 后把这个错位置写回 localStorage，正确位置就此丢掉。
  try {
    const saved = JSON.parse(localStorage.getItem(LS_POS + store.currentId) || '{}')
    if (saved.fit) fit.value = saved.fit
    if (saved.zoom) zoom.value = saved.zoom
  } catch { /* */ }
  await load()
  await nextTick()
  await measureNotes()
  ro = new ResizeObserver(() => { reflow() })
  ro.observe(deskEl.value.parentElement || deskEl.value)
  document.addEventListener('mouseup', onMouseUp)
  document.addEventListener('mousedown', onDocDown)
  window.addEventListener('resize', updateMid)
  scroller().addEventListener('scroll', onScroll, { passive: true })
  scroller().addEventListener('wheel', onUserScroll, { passive: true })
  scroller().addEventListener('wheel', onWheelZoom, { passive: false })   // Ctrl+滚轮要 preventDefault，不能 passive
  scroller().addEventListener('touchstart', onUserScroll, { passive: true })
})

onBeforeUnmount(() => {
  selStream?.abort()
  ro?.disconnect()
  document.removeEventListener('mouseup', onMouseUp)
  document.removeEventListener('mousedown', onDocDown)
  window.removeEventListener('resize', updateMid)
  scroller()?.removeEventListener('scroll', onScroll)
  scroller()?.removeEventListener('wheel', onUserScroll)
  scroller()?.removeEventListener('wheel', onWheelZoom)
  scroller()?.removeEventListener('touchstart', onUserScroll)
  stopCreep()
  for (const d of Object.values(docs)) { try { d?.destroy() } catch { /* */ } }
  docs = { orig: null, dual: null, mono: null }
})

// ---------- 框选视觉问答 ----------
const frameRect = ref(null)

function cropItem(it, r) {
  const canvas = canvases.value[it.gi]
  const dpr = canvas.width / parseFloat(canvas.style.width)
  const sx = r.x0 * dpr, sy = r.y0 * dpr, sw = (r.x1 - r.x0) * dpr, sh = (r.y1 - r.y0) * dpr
  const off = document.createElement('canvas')
  off.width = Math.max(2, sw); off.height = Math.max(2, sh)
  off.getContext('2d').drawImage(canvas, sx, sy, sw, sh, 0, 0, off.width, off.height)
  return off.toDataURL('image/jpeg', 0.85)
}

const vis = reactive({ visible: false, x: 0, y: 0, img: '', question: '', answer: '', busy: false, err: '',
                       page: 0, paraIdx: 0, rect: null })

function startFrameDrag(e, it) {
  if (!store.viewer.frame || e.button !== 0) return
  // 译文/双语页（origPage = -1）上不许框选：那里没有原文段落，页边也摆不出对应的卡片，
  // 钉下去的结果是"AI 的回答存了但永远看不到"，还会顺手覆盖 ¶0 上已有的那条。
  if (it.origPage < 0) { toast('译文页上不能框选：切回「原文」再圈，钉的位置才对得上'); return }
  e.preventDefault()
  const el = pageEls.value[it.gi]
  const base = el.getBoundingClientRect()
  const pt = ev => ({ x: ev.clientX - base.left, y: ev.clientY - base.top })
  const p0 = pt(e)
  const move = ev => { const p = pt(ev); frameRect.value = { x0: p0.x, y0: p0.y, x1: p.x, y1: p.y, gi: it.gi } }
  const stop = () => {
    document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up)
    window.removeEventListener('blur', stop)
  }
  const up = ev => {
    stop()
    const rr = frameRect.value
    frameRect.value = null
    if (!rr) return
    const x0 = Math.min(rr.x0, rr.x1), y0 = Math.min(rr.y0, rr.y1)
    const x1 = Math.max(rr.x0, rr.x1), y1 = Math.max(rr.y0, rr.y1)
    if (x1 - x0 < 24 || y1 - y0 < 24) return
    const img = cropItem(it, { x0, y0, x1, y1 })
    // 记住这是哪一页、哪一段，钉页边时才落得准
    const s = scale.value
    let paraIdx = 0
    for (const p of parasByPage.value[it.origPage] || []) {
      if (y0 / s >= p.bbox.y0 - 3 && y0 / s <= p.bbox.y1 + 3) { paraIdx = p.idx; break }
    }
    Object.assign(vis, { visible: true, x: Math.min(window.innerWidth - 410, ev.clientX + 12),
                         y: Math.min(window.innerHeight - 340, Math.max(64, ev.clientY - 60)),
                         img, question: '解释选区里的内容。', answer: '', busy: false, err: '',
                         page: it.origPage, paraIdx,
                         rect: it.origPage >= 0 ? { x0: x0 / s, y0: y0 / s, x1: x1 / s, y1: y1 / s } : null })
  }
  frameRect.value = { x0: p0.x, y0: p0.y, x1: p0.x, y1: p0.y }
  document.addEventListener('mousemove', move); document.addEventListener('mouseup', up)
  // 拖到窗口外松手时 document 收不到 mouseup：监听器会一直留着，之后鼠标一动纸上就冒
  // 幽灵框（下一次框选的结果还可能是上一次的）。失焦也当成"松手了"。
  window.addEventListener('blur', stop)
}

function closeVis() { vis.visible = false }

async function askVisual() {
  if (!vis.question.trim() || vis.busy) return
  vis.busy = true; vis.err = ''
  try {
    const r = await api.askVisual({ image: vis.img, question: vis.question })
    vis.answer = r.answer
  } catch (e) { vis.err = e.message }
  vis.busy = false
}

async function pinVisual() {
  if (vis.busy) return
  if (!vis.answer) await askVisual()
  if (!vis.answer) return
  await api.pin(store.currentId, {
    quote: '[选区] ' + vis.question.slice(0, 60), note: vis.answer.slice(0, 560),
    para_idx: vis.paraIdx, page: vis.page, rect: vis.rect,
  })
  await refreshM()
  closeVis()
  toast('已钉在页边')
}

watch(() => store.visPrefill, pf => {
  if (!pf) return
  Object.assign(vis, { visible: true, x: Math.max(60, Math.floor(midX.value || window.innerWidth / 2) - 190), y: 90,
                       img: pf.img, question: pf.question || '讲解这张图。', answer: '', busy: false, err: '',
                       page: pf.page ?? 0, paraIdx: pf.paraIdx ?? 0, rect: pf.rect ?? null })
  store.visPrefill = null
  askVisual()
})

/* 点空白处收起浮层；Esc 交给 store.escTick */
function onDocDown(e) {
  const t = e.target
  if (!t?.closest) return
  if (vis.visible && !t.closest('.vis-pop')) closeVis()
  // 划词气泡：点它以外任何地方都收（包括纸面本身）。
  // 原来把 .textLayer 排除在外，本意是"别把正在划的词弄丢"，结果是在纸上点哪儿都不关，
  // 只能去够那个小叉。其实点下去会清掉选区、mouseup 又会按新选区重开气泡，不会丢东西。
  if (sel.visible && !t.closest('.sel-pop')) closeSel()
}
watch(() => store.escTick, () => {
  closeSel()
  if (vis.visible) closeVis()
  if (searchOpen.value) closeSearch()
})
// 聚焦要用 preventScroll：查找框挂在书桌内容的末尾，浏览器为了"把焦点滚进视野"
// 会把整个书桌滚到底，把刚跳好的命中位置冲掉。
watch(searchOpen, v => { if (v) nextTick(() => searchInputEl.value?.focus({ preventScroll: true })) })
let searchT = null
watch(searchQ, () => { clearTimeout(searchT); searchT = setTimeout(runSearch, 320) })

watch(() => store.viewer.variant, () => { doneKeys.clear(); load({ keepPlace: true }); saveLater() })
watch(() => store.viewer.spread, () => { doneKeys.clear(); load({ keepPlace: true }); saveLater() })
watch(scale, () => { doneKeys.clear(); scheduleRender(); saveLater() })
watch(() => store.paras, () => { spanCache.clear() })
watch(() => store.jump, applyJump)
// 图层开关（眉批/略读）会改页边宽度和标注的密度，换完要重新定标落回原处
watch(() => store.viewer.layers, () => reflow(), { deep: true })
// 右栏/文库拖宽结束时不需要 ResizeObserver 的延迟：直接重排一次
watch(() => store.reflowTick, () => reflow())
watch(() => store.marginalia.notes, (n, o) => {
  spanCache.clear()
  if (n.length && (!o || n.length > o.length)) {
    freshNotes.value = true
    setTimeout(() => (freshNotes.value = false), 1600)
  }
  measureNotes()
})
</script>

<template>
  <div class="desk-inner" ref="deskEl">
    <Transition name="desk" mode="out-in">
    <div class="hatch" v-if="!ready">
      <EggMark class="hatch-mark" />
      <div class="hatch-line"><i :style="{ width: Math.round(loadPct * 100) + '%' }" /></div>
      <div class="hatch-word">正在破壳</div>
    </div>

    <div v-else class="sheet-stage">
      <div class="spread-row" v-for="(s, si) in sheets" :key="si">
        <div class="page-wrap" v-for="it in s.items" :key="it.key">
            <div class="page" :ref="el => (pageEls[it.gi] = el)"
                 :style="{ width: it.w * scale + 'px', height: it.h * scale + 'px' }"
                 @mousedown="e => startFrameDrag(e, it)">

            <canvas :ref="el => (canvases[it.gi] = el)"></canvas>
            <div class="care-wash" v-if="store.viewer.care !== 'off'"></div>
            <div class="textLayer" v-if="it.text && !store.viewer.frame" :ref="el => (textLayers[it.gi] = el)"></div>
            <div v-if="frameRect && frameRect.gi === it.gi" class="frame-rect"
                 :style="{ left: Math.min(frameRect.x0, frameRect.x1) + 'px', top: Math.min(frameRect.y0, frameRect.y1) + 'px',
                           width: Math.abs(frameRect.x1 - frameRect.x0) + 'px', height: Math.abs(frameRect.y1 - frameRect.y0) + 'px' }"></div>

            <div class="para-zone">
              <template v-for="(p, pi) in parasByPage[it.origPage] || []" :key="'f' + p.idx">
                <!-- 蒙掉的段落：悬停掀开看一眼（CSS），点一下=「这段我也要读」 -->
                <div v-if="veiled(p, it.origPage)"
                     class="para-fade veil"
                     :title="`略读把这一段蒙掉了（判为「${roleTag(p)}」）· 点一下：这段也要读`"
                     :style="{ ...rectStyle(p), animationDelay: Math.min(400, pi * 12) + 'ms' }"
                     @click="toggleKeep(p.idx)"></div>
                <!-- 留下来的段落：左侧一道芯线 + 纸边一行角色名，说清"为什么留它" -->
                <template v-else-if="store.viewer.layers.skim && it.origPage >= 0 && (isCore(p) || kept(p.idx))">
                  <div class="para-core-bar"
                       :style="{ top: p.bbox.y0 * scale + 'px', height: (p.bbox.y1 - p.bbox.y0) * scale + 'px' }"></div>
                  <span class="para-tag" :style="{ top: p.bbox.y0 * scale + 'px' }">{{ roleTag(p) }}</span>
                </template>
                <div v-if="flash?.idx === p.idx && flash?.gi === it.gi" class="para-fade hot" :style="rectStyle(p)"></div>
              </template>

              <!-- 跳转/查找命中：精确到字符的矩形，落在哪就亮在哪 -->
              <div v-for="(b, bi) in flash?.gi === it.gi ? flash.boxes || [] : []" :key="'fb' + bi"
                   class="para-fade hot boxed" :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              <template v-for="(h, hi) in searchHitsOnPage(it.origPage)" :key="'s' + hi">
                <div v-for="(b, bi) in h.rects" :key="bi" class="find-hit" :class="{ cur: h === currentHit }"
                     :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              </template>

              <!-- 眉批引文：按句子落行，划了几行就是几个块；框选钉子按区域画。
                   笔法分三档（见 styles.css）：值得读是马克笔、要当心是波浪线、可跳过只有一条点线。
                   块本身不吃鼠标事件——它盖在正文上，吃了就没法选字了。 -->
              <template v-for="{ n } in notesOnPage(it.origPage)" :key="'n' + n.id">
                <div v-for="(b, bi) in markBoxes(n)" :key="bi" class="mg-mark" :data-nid="n.id"
                     :class="['b-' + bandOf(n), { draw: freshNotes, hot: hotNote === n.id, loose: quoteLoose(n) }]"
                     :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              </template>
            </div>
          </div>

          <!-- 页边批注带：宽度跟着"有没有东西要放"走 -->
          <div class="gutter" v-if="it.margin && gutterW > 0"
               :style="{ height: (pageLayouts[it.origPage]?.height || it.h * scale) + 'px',
                         width: gutterW + 'px', marginLeft: gutterPad + 'px' }">
            <div v-for="{ n, top, pending } in notesOnPage(it.origPage)" :key="'mg' + n.id"
                 class="mg-note" :data-nid="n.id"
                 :style="{ top: top + 'px', borderLeftColor: kindColor(n),
                           width: Math.max(120, gutterW - 30) + 'px' }"
                 :class="{ fresh: freshNotes, pending, ping: pingId === n.id,
                           expanded: expandedNote === n.id,
                           allq: openQuote === n.id,
                           clamped: (n.note || '').length > 34, clampable: (n.note || '').length > 34 }"
                 @mouseenter="hoverNote(n.id)" @mouseleave="hoverNote(null)"
                 @click="toggleNote(n)">
              <div class="mg-head">
                <span class="mg-dot" :style="{ background: kindColor(n) }"></span>
                <span class="mg-kind" :style="{ color: kindText(n) }">
                  {{ pending ? '翻译中' : kindLabel(n) }}
                </span>
                <span v-if="(n.note || '').length > 34" class="mg-more">{{ expandedNote === n.id ? '收起' : '展开' }}</span>
                <!-- 就地追问：读到这条批注时人的第一反应是"凭什么"，
                     追问要在这儿，而不是跳到右栏问题页去凑一句话 -->
                <button v-if="!pending" class="mg-ask" title="就这条批注追问模型"
                        @click.stop="askNote(n)">问 ↗</button>
                <button v-if="!pending" class="mg-del" title="移除这条批注" @click.stop="unpin(n.id)">×</button>
              </div>
              <div class="mg-body">{{ prettyChem(n.note) }}</div>
              <!-- 引文：默认看开头，点「全句」摊开；引文本身点了是跳回纸上那句 -->
              <div class="mg-quote-row">
                <span class="mg-quote" :class="{ all: openQuote === n.id }"
                      :title="'跳到纸上这句：' + anchorText(n)" @click.stop="jumpQuote(n)">“{{ quoteShown(n) }}”</span>
                <button v-if="anchorText(n).length > 44" class="mg-qall" @click.stop="toggleQuote(n)">
                  {{ openQuote === n.id ? '收起' : '全句' }}
                </button>
                <!-- 模型引文和原文对不齐时说实话：划线只盖对得上的部分 -->
                <span v-if="quoteLoose(n)" class="mg-loose" title="模型抄回的引文与原文略有出入，纸上的划线只盖对得上的那一段">≈</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    </Transition>

    <!-- 出图进度：一条不挡路的细线，比"遮住论文的加载器"诚实 -->
    <div class="stage-line" v-if="ready && rendering"><i /></div>

    <!-- 阅读进度：贴书桌右缘的一根细线，读到哪长到哪 -->
    <div class="read-prog" v-if="ready" :style="{ left: deskRightX + 'px' }"><i :style="{ height: progPct * 100 + '%' }" /></div>


    <!-- 阅读器控件：翻页 / 缩放 / 查找。整条可拖走，别压在论文中间 -->
    <Transition name="fade">
    <div v-if="ready" class="desk-float zoom-bar" :style="{ left: midX + 'px' }"
         v-drag="{ key: 'zoombar' }" data-drag>
      <button title="上一页（PageUp）" @click="stepPage(-1)">‹</button>
      <span class="zb-page">
        <input ref="pageInputEl" v-model="pageIn" class="zb-input" title="跳到第几页"
               @keydown.enter="gotoPage(Number(pageIn))" @blur="pageIn = String(pageNum)" />
        <em>/ {{ store.paper?.n_pages || 0 }}</em>
      </span>
      <button title="下一页（PageDown）" @click="stepPage(1)">›</button>
      <span class="zb-sep"></span>
      <button title="适应宽度" :class="{ on: fit === 'width' }" @click="setFit('width')">适宽</button>
      <button title="适应页面" :class="{ on: fit === 'page' }" @click="setFit('page')">适页</button>
      <button class="zb-num" title="实际大小" @click="setFit('none')">{{ zoomPct }}%</button>
      <button title="缩小" @click="stepZoom(-1)">－</button>
      <button title="放大" @click="stepZoom(1)">＋</button>
      <span class="zb-sep"></span>
      <button title="在论文里查找（Ctrl+F）" :class="{ on: searchOpen }" @click="searchOpen = !searchOpen">查找</button>
    </div>
    </Transition>

    <!-- 文档内查找：结果条数与位置都来自真实字符矩形 -->
    <Transition name="pop">
    <div v-if="ready && searchOpen" class="desk-float find-bar" :style="{ left: midX + 'px' }"
         v-drag="{ key: 'findbar' }" data-drag>
      <input ref="searchInputEl" v-model="searchQ" class="fb-input" placeholder="在论文里找…"
             @keydown.enter="searchStep(1)" @keydown.escape="closeSearch" />
      <span class="fb-count">
        {{ searchHits.length ? (searchAt + 1) + ' / ' + searchHits.length : (searchBusy ? '…' : '无结果') }}
      </span>
      <button :disabled="!searchHits.length" title="上一个（Enter）" @click="searchStep(-1)">‹</button>
      <button :disabled="!searchHits.length" title="下一个（Enter）" @click="searchStep(1)">›</button>
      <button class="ghost" title="关闭（Esc）" @click="closeSearch">×</button>
    </div>
    </Transition>

    <!-- 框选中：常驻退出口。顶栏那颗「框选」此刻正亮着，这里不必再把同一个词说一遍 -->
    <Transition name="pop">
    <div v-if="ready && store.viewer.frame" class="frame-hint desk-float" :style="{ left: midX + 'px' }">
      <button @click="store.viewer.frame = false">退出框选</button>
    </div>
    </Transition>

    <!-- 略读的状态行：略读是按段落角色蒙纱，读者得知道它蒙了几段、留了几段、
         还有几段根本没判过；判错了怎么扳回来也写在这里。没析读时它直说"判不了"，
         而不是默默什么都不做（那看起来就是"略读坏了"）。 -->
    <Transition name="fade">
    <div v-if="ready && store.viewer.layers.skim" class="desk-float skim-hud" :style="{ left: midX + 'px' }">
      <template v-if="store.analysis.status === 'done'">
        <span>保留 <b class="mono-num">{{ skimStats.kept }}</b> 段 · 略去 <b class="mono-num">{{ skimStats.skipped }}</b> 段</span>
        <span v-if="skimStats.byNote" class="sh-note"
              title="这些段上有「值得读 / 要当心」的眉批——有批注的段不蒙，否则刚写的提醒会被一起藏起来">含 {{ skimStats.byNote }} 段有眉批</span>
        <span v-if="skimStats.unknown" class="sh-warn"
              title="这些段没有角色（析读没判到，或你手工改判过），按原文显示，不会被蒙掉">未判 {{ skimStats.unknown }} 段</span>
        <button v-if="skimStats.mine" title="取消你手选的「这段也要读」" @click="clearKeep">手选 {{ skimStats.mine }} 段 ✕</button>
        <span class="sh-tip">点蒙掉的段 = 这段也要读</span>
      </template>
      <span v-else>略读要按角色蒙纱，先点顶栏「析读」</span>
      <button @click="store.viewer.layers.skim = false">退出略读</button>
    </div>
    </Transition>

    <!-- 划词气泡 -->
    <Transition name="pop">
    <div class="sel-pop" v-if="sel.visible" :style="{ left: sel.x + 'px', top: sel.y + 'px' }"
         v-drag="{ key: 'selpop' }" data-drag @mouseup.stop>
      <div v-if="!sel.zh && !sel.busy && !sel.err" style="font-size:var(--fs-sm);color:var(--ink-3)">
        已选 {{ sel.text.length }} 字符<span v-if="sel.paraIdx >= 0" class="mono-num"> · ¶{{ sel.paraIdx }}</span>
      </div>
      <div v-if="sel.busy && !sel.zh" style="font-size:var(--fs-sm);color:var(--ink-3)">翻译中…</div>
      <div v-if="sel.err && !sel.zh" style="font-size:var(--fs-sm);color:var(--vermilion)">{{ sel.err }}</div>
      <div class="sp-zh" v-if="sel.zh">{{ sel.zh }}<span v-if="sel.busy" class="qa-caret"></span></div>
      <div class="sp-hits" v-if="sel.hits.length">
        <span class="chip" v-for="h in sel.hits" :key="h.en">📌 {{ h.en }} → {{ h.zh }}</span>
      </div>
      <!-- 自己写一条：页边也是你的本子，不只是 AI 说话的地方 -->
      <div class="sp-mine" v-if="mine.open">
        <textarea ref="mineEl" v-model="mine.text" rows="3" placeholder="就这句写点什么…（Ctrl+Enter 保存）"
                  @mouseup.stop @keydown.enter.ctrl="saveMine"></textarea>
      </div>
      <div class="sp-actions">
        <template v-if="mine.open">
          <button class="primary" style="padding:4px 10px" :disabled="!mine.text.trim()" @click="saveMine">写到页边</button>
          <button style="padding:4px 10px" @click="mine.open = false">取消</button>
        </template>
        <template v-else>
          <button class="primary" style="padding:4px 10px" @click="doTranslateSel" :disabled="sel.busy">
            {{ sel.busy ? '翻译中' : (sel.zh ? '重译' : '翻译') }}
          </button>
          <button v-if="sel.zh" style="padding:4px 10px"
                  @click="copySel">复制译文</button>
          <button style="padding:4px 10px" @click="pinSel">钉在页边</button>
          <button style="padding:4px 10px" @click="openMine">写批注</button>
          <button style="padding:4px 10px" @click="sendToGlossary">收进术语</button>
          <button style="padding:4px 10px" @click="askAboutSel">提问</button>
          <button class="ghost" style="padding:4px 8px" @click="closeSel()">×</button>
        </template>
      </div>
    </div>
    </Transition>

    <!-- 返回原位 -->
    <!-- 框选视觉问答 -->
    <Transition name="pop">
    <div class="sel-pop vis-pop" v-if="vis.visible" :style="{ left: vis.x + 'px', top: vis.y + 'px' }"
         v-drag="{ key: 'vispop' }" data-drag @mouseup.stop>
      <div class="vp-head">
        <span class="mono-label">选区问 AI</span>
        <button class="vp-x" title="关闭（Esc）" @click="closeVis">×</button>
      </div>
      <img class="vis-img" :src="vis.img" />
      <input type="text" v-model="vis.question" style="width:100%; margin-top:8px"
             @keydown.enter="askVisual" placeholder="问这个选区…" />
      <div class="sp-actions">
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="() => { vis.question = '讲解这张图：画了什么、支持什么结论'; askVisual() }">讲解此图</button>
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="() => { vis.question = '这个公式每一步的含义和推导逻辑'; askVisual() }">讲公式</button>
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="() => { vis.question = '挖一下这张表里的数据：趋势、异常和可疑之处'; askVisual() }">挖表格</button>
      </div>
      <div v-if="vis.busy" class="vp-state">正在看图</div>
      <div v-if="vis.err" class="vp-state err">{{ vis.err }}</div>
      <MdLite v-if="vis.answer" class="vp-answer" :text="vis.answer" />
      <!-- 关掉的出口只有右上角那个 ×（和 Esc）：左下角再挂一个"关闭"是重复，
           底部只留真正要做的动作 -->
      <div class="sp-actions" v-if="vis.answer">
        <button class="primary" style="padding:4px 10px" @click="pinVisual">钉在页边</button>
      </div>
    </div>
    </Transition>

    <Transition name="pop">
      <button class="back-chip desk-float" v-if="backChip" :style="{ left: midX + 'px' }" @click="jumpBack">
        返回原位 · Alt+←
      </button>
    </Transition>
  </div>
</template>
