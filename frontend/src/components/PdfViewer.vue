<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, KIND_ZH, ROLE_ZH, ROLE_GLYPH, CORE_ROLES, KIND_COLOR, ROLE_COLOR,
         ROLE_TEXT_COLOR, KIND_TEXT_COLOR, roleInk } from '../store'
import { lineSpanOf, findQuoteRects, findAllRects, clearTextIndex } from '../find'
import MdLite from './MdLite.vue'
import { vDrag } from '../drag'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

defineEmits(['override'])

const GUTTER = 196        // 184 的批注带 + 12 的左边距，measure() 要按整宽算
const LS_POS = 'eggpaper:pos:'

const deskEl = ref(null)
const ready = ref(false)
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
const sheets = ref([])
const roleCard = ref(null)       // { idx, x, y } 点开的角色卡，只有点击能开关
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
const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))
const notesShown = computed(() => (store.viewer.layers.marginalia ? store.marginalia.notes : []))
const flatItems = computed(() => sheets.value.flatMap(s => s.items))
const tranReady = computed(() => store.paper?.translate_status === 'done')
const isSpread = computed(() => store.viewer.variant === 'dual' && store.viewer.spread === 'spread')

function roleOf(p) {
  return store.viewer.layers.skeleton ? store.analysis.annotations[String(p.idx)]?.role : null
}
function isCore(p) { return CORE_ROLES.includes(roleOf(p) || 'background') }

/* ---------------- 文档装载与 sheets 构建 ---------------- */

async function getDoc(kind) {
  if (!docs[kind]) docs[kind] = await pdfjsLib.getDocument(`/api/papers/${store.currentId}/pdf?variant=${kind}`).promise
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

async function load({ keepPlace = false } = {}) {
  loading = true
  const veryFirst = !sheets.value.length
  if (veryFirst) ready.value = false
  // 换姿势（原文/译文/双语/对开）前先记住读到哪里，换完再落回同一页同一高度
  const anchor = keepPlace || !veryFirst ? currentAnchor() : null
  sheets.value = []
  doneKeys.clear()
  try {
    await buildSheets()
  } catch (e) {
    toast('文档加载失败：' + e.message)
    ready.value = true
    loading = false
    return
  }
  await measure()
  await renderAll()
  ready.value = true
  await nextTick()
  await measureNotes()
  if (anchor) applyAnchor(anchor)
  else if (store.viewer.restorePos) { scroller().scrollTop = store.viewer.restorePos; store.viewer.restorePos = 0 }
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
  const gutterW = store.viewer.variant === 'original' ? GUTTER : 6
  // 量的是滚动容器（书桌）的宽度，不是 .desk-inner——后者是 max-content，
  // 会随排版自己长大，拿它算"适宽"就成了正反馈：纸越大 → 容器越宽 → 纸更大。
  fitScale.value = (sc.clientWidth - 60 - gutterW - (perRow === 2 ? 20 : 0)) / (first.w * perRow)
  // 适页：把一整页塞进书桌高度（留出上下内边距）
  pageFitScale.value = (sc.clientHeight - 56) / first.h
}

function updateMid() {
  const el = deskEl.value?.closest('.desk') || deskEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  midX.value = Math.round(r.left + r.width / 2)
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

function tabsOnPage(pno) {
  const out = []
  let prevBottom = -1
  for (const p of parasByPage.value[pno] || []) {
    const role = roleOf(p)
    if (!role) continue
    let top = p.bbox.y0 * scale.value
    if (top < prevBottom + 3) top = prevBottom + 3
    prevBottom = top + 25
    out.push({ p, role, top })
  }
  return out
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
function quoteSpan(n) {
  if (!n) return null
  const k = n.id + '|' + n.para_idx
  if (!spanCache.has(k)) {
    const p = paraByIdx.value[n.para_idx]
    spanCache.set(k, n.quote && p ? lineSpanOf(p, n.quote) : null)
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
function computeQuoteMarks() {
  const out = {}
  for (const n of notesShown.value) {
    if (isRegion(n) || !n.quote) continue
    const it = pageItem(n.page)
    const el = it && pageEls.value[it.gi]
    if (!el) continue
    const r = findQuoteRects(el, n.quote)
    if (r) out[n.id] = r.rects
  }
  quoteMarks.value = out
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

/* ---------------- 角色卡：点击开，Esc/点外/×关 ---------------- */

let roleCardAnchor = null        // 打开卡片的那个书签元素
let roleCardRaf = 0
const roleCardEl = ref(null)
let roleCardDragged = false      // 用户把它拖走了：就别再粘回书签旁边（抢不过人手）

// 卡片跟着书签走：页面一滚就重新贴回书签旁边，而不是被滚没了
function placeRoleCard() {
  if (roleCard.value == null || !roleCardAnchor || roleCardDragged) return
  const r = roleCardAnchor.getBoundingClientRect()
  const desk = (deskEl.value?.closest('.desk') || deskEl.value)?.getBoundingClientRect()
  if (desk && (r.bottom < desk.top - 40 || r.top > desk.bottom + 40)) { closeRoleCard(); return }
  const h = roleCardEl.value?.offsetHeight || 170
  roleCard.value = {
    idx: roleCard.value.idx,
    x: Math.max(12, r.left - 226 - 8),
    y: Math.min(Math.max(desk ? desk.top + 10 : 64, r.top - 10), window.innerHeight - h - 14),
  }
}
function followRoleCard() {
  if (roleCard.value == null) return
  cancelAnimationFrame(roleCardRaf)
  roleCardRaf = requestAnimationFrame(placeRoleCard)
}

async function openRoleCard(e, p) {
  if (roleCard.value?.idx === p.idx) { closeRoleCard(); return }
  roleCardAnchor = e.currentTarget
  roleCardDragged = false
  roleCard.value = { idx: p.idx, x: 0, y: 0 }
  expandedNote.value = null
  await nextTick()
  placeRoleCard()
}
function closeRoleCard() {
  roleCard.value = null
  roleCardAnchor = null
  roleCardDragged = false
}

const roleCardData = computed(() => {
  const idx = roleCard.value?.idx
  if (idx == null) return null
  const p = paraByIdx.value[idx]
  const anno = store.analysis.annotations[String(idx)]
  if (!p || !anno) return null
  return { p, anno, role: anno.role }
})

function toggleNote(n) {
  if ((n.note || '').length <= 34) return
  expandedNote.value = expandedNote.value === n.id ? null : n.id
  measureNotes()
}

async function translateParaAndPin(idx) {
  if (pendingPara.value != null) return
  pendingPara.value = idx
  try {
    const r = await api.translatePara(store.currentId, idx)
    if (!r.zh || !r.zh.trim()) { toast('模型没返回内容，再试一次'); return }
    const p = paraByIdx.value[idx]
    await api.pin(store.currentId, { quote: (p?.text || '').slice(0, 150), note: r.zh, para_idx: idx, page: p?.page ?? 0 })
    await refreshM()
    toast('译文已钉在页边')
  } catch (e) { toast('翻译失败：' + e.message) }
  finally { pendingPara.value = null }
}

/* ---------------- 划词 ---------------- */

const sel = reactive({ visible: false, x: 0, y: 0, text: '', context: '', paraIdx: 0, page: 0, zh: '', hits: [], busy: false, err: '' })

function onMouseUp(e) {
  const s = window.getSelection()
  if (!s || s.isCollapsed || !s.rangeCount) return
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
}

async function doTranslateSel() {
  sel.busy = true; sel.err = ''
  try {
    const r = await api.translateSelection(store.currentId, sel.text, sel.context)
    sel.zh = r.zh; sel.hits = r.hits
  } catch (e) { sel.err = e.message }
  sel.busy = false
}

async function pinSel() {
  if (!sel.zh) await doTranslateSel()
  if (!sel.zh) return
  await api.pin(store.currentId, { quote: sel.text.slice(0, 150), note: sel.zh, para_idx: sel.paraIdx, page: sel.page })
  await refreshM()
  sel.visible = false
  toast('已钉在页边')
}

function sendToGlossary() {
  store.glossaryPrefill = { term_en: sel.text.slice(0, 80), term_zh: (sel.zh || '').replace('〔演示译文〕', '').slice(0, 24) }
  sel.visible = false
  toast('已带到术语表，请确认中文译法')
  window.dispatchEvent(new CustomEvent('eggpaper:terms-prefill'))
}

function askAboutSel() {
  store.askPrefill = { text: sel.text.slice(0, 80) }
  sel.visible = false
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
function hitBoxes(h) { return h?.rects || [] }
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
  setTimeout(() => (flash.value = null), 2200)
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
  else toast('滚动一下，告诉我你在读哪段')
}

store.viewerApi = { step, translateCurrent, jumpBack, translateSelectionKey, stepPage, gotoPage, openSearch }

function openSearch() { searchOpen.value = true }

/* ---------------- 滚动：scroll-spy + 位置记忆 ---------------- */

let spyT = null, saveT = null
function onScroll() {
  followRoleCard()          // 卡片跟着书签走，不再一滚就消失
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
  await load()
  await nextTick()
  try {
    const saved = JSON.parse(localStorage.getItem(LS_POS + store.currentId) || '{}')
    if (saved.fit) fit.value = saved.fit
    if (saved.zoom) zoom.value = saved.zoom
  } catch { /* */ }
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
  ro?.disconnect()
  cancelAnimationFrame(roleCardRaf)
  document.removeEventListener('mouseup', onMouseUp)
  document.removeEventListener('mousedown', onDocDown)
  window.removeEventListener('resize', updateMid)
  scroller()?.removeEventListener('scroll', onScroll)
  scroller()?.removeEventListener('wheel', onUserScroll)
  scroller()?.removeEventListener('wheel', onWheelZoom)
  scroller()?.removeEventListener('touchstart', onUserScroll)
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
  e.preventDefault()
  const el = pageEls.value[it.gi]
  const base = el.getBoundingClientRect()
  const pt = ev => ({ x: ev.clientX - base.left, y: ev.clientY - base.top })
  const p0 = pt(e)
  const move = ev => { const p = pt(ev); frameRect.value = { x0: p0.x, y0: p0.y, x1: p.x, y1: p.y, gi: it.gi } }
  const up = ev => {
    document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up)
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
  if (roleCard.value != null && !t.closest('.role-card') && !t.closest('.role-tab')) roleCard.value = null
  if (vis.visible && !t.closest('.vis-pop')) closeVis()
  if (sel.visible && !t.closest('.sel-pop') && !t.closest('.textLayer')) sel.visible = false
}
watch(() => store.escTick, () => {
  sel.visible = false
  if (vis.visible) closeVis()
  roleCard.value = null
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
    <div class="reading" v-if="!ready" style="max-width:420px;margin:60px auto">
      <div class="r-line">正在摆上书桌<span class="r-dots">…</span></div>
      <div class="r-bar"><i /></div>
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
                <div v-if="store.viewer.layers.skim && it.origPage >= 0 && roleOf(p) && !isCore(p)"
                     class="para-fade"
                     :style="{ ...rectStyle(p), transitionDelay: Math.min(400, pi * 12) + 'ms' }"></div>
                <div v-else-if="store.viewer.layers.skim && it.origPage >= 0 && roleOf(p) && isCore(p)"
                     class="para-core-bar"
                     :style="{ top: p.bbox.y0 * scale + 'px', height: (p.bbox.y1 - p.bbox.y0) * scale + 'px' }"></div>
                <div v-if="flash?.idx === p.idx && flash?.gi === it.gi" class="para-fade hot" :style="rectStyle(p)"></div>
              </template>

              <!-- 跳转/查找命中：精确到字符的矩形，落在哪就亮在哪 -->
              <div v-for="(b, bi) in flash?.gi === it.gi ? flash.boxes || [] : []" :key="'fb' + bi"
                   class="para-fade hot" :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              <template v-for="(h, hi) in searchHitsOnPage(it.origPage)" :key="'s' + hi">
                <div v-for="(b, bi) in h.rects" :key="bi" class="find-hit" :class="{ cur: h === currentHit }"
                     :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              </template>

              <!-- 眉批引文：按句子落行，划了几行就是几个块；框选钉子按区域画 -->
              <template v-for="{ n } in notesOnPage(it.origPage)" :key="'n' + n.id">
                <div v-for="(b, bi) in markBoxes(n)" :key="bi" class="mg-mark" :data-nid="n.id"
                     :class="{ draw: freshNotes }"
                     :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px',
                               background: KIND_COLOR[n.kind] + '2e',
                               borderBottom: '2px solid ' + KIND_COLOR[n.kind] + '99' }"></div>
              </template>
            </div>
          </div>

          <!-- 页边批注带 -->
          <div class="gutter" v-if="it.margin"
               :style="{ height: (pageLayouts[it.origPage]?.height || it.h * scale) + 'px' }">
            <div v-for="{ p, role, top } in tabsOnPage(it.origPage)" :key="'t' + p.idx"
                 class="role-tab" :class="{ on: roleCard?.idx === p.idx }"
                 :style="{ top: top + 'px', background: ROLE_COLOR[role], color: roleInk(role) }"
                 :title="`¶${p.idx} · ${ROLE_ZH[role]}`"
                 @click.stop="openRoleCard($event, p)">
              <span class="pn">{{ ROLE_GLYPH[role] }}</span>
            </div>
            <div v-for="{ n, top, pending } in notesOnPage(it.origPage)" :key="'mg' + n.id"
                 class="mg-note" :data-nid="n.id"
                 :class="{ fresh: freshNotes, pending, expanded: expandedNote === n.id,
                           clamped: (n.note || '').length > 34, clampable: (n.note || '').length > 34 }"
                 :style="{ top: top + 'px', borderLeftColor: KIND_COLOR[n.kind] }"
                 @click="toggleNote(n)">
              <div class="mg-head">
                <span class="mg-kind" :style="{ color: KIND_TEXT_COLOR[n.kind] }">
                  {{ pending ? '翻译中' : (KIND_ZH[n.kind] ? (n.kind === 'lookup' || n.kind === 'region' ? '你 · ' : '') + KIND_ZH[n.kind] : '') }}
                </span>
                <span v-if="(n.note || '').length > 34" class="mg-more">{{ expandedNote === n.id ? '收起' : '展开' }}</span>
                <button v-if="!pending" class="mg-del" title="移除这条批注" @click.stop="unpin(n.id)">×</button>
              </div>
              <div class="mg-body">{{ n.note }}</div>
              <span class="mg-quote" :title="'跳到纸上这句：' + n.quote" @click.stop="jumpQuote(n)">“{{ n.quote.slice(0, 40) }}{{ n.quote.length > 40 ? '…' : '' }}”</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 出图进度：一条不挡路的细线，比"遮住论文的加载器"诚实 -->
    <div class="stage-line" v-if="ready && rendering"><i /></div>

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

    <!-- 框选中：常驻提示 + 退出口 -->
    <Transition name="pop">
    <div v-if="ready && store.viewer.frame" class="frame-hint desk-float" :style="{ left: midX + 'px' }">
      <span class="fh-tag">框选</span>
      <button @click="store.viewer.frame = false">退出</button>
    </div>
    </Transition>

    <!-- 划词气泡 -->
    <Transition name="pop">
    <div class="sel-pop" v-if="sel.visible" :style="{ left: sel.x + 'px', top: sel.y + 'px' }"
         v-drag="{ key: 'selpop' }" data-drag @mouseup.stop>
      <div v-if="!sel.zh && !sel.busy && !sel.err" style="font-size:var(--fs-sm);color:var(--ink-3)">
        已选 {{ sel.text.length }} 字符
      </div>
      <div v-if="sel.busy" style="font-size:var(--fs-sm);color:var(--ink-3)">翻译中…</div>
      <div v-if="sel.err" style="font-size:var(--fs-sm);color:var(--vermilion)">{{ sel.err }}</div>
      <div class="sp-zh" v-if="sel.zh">{{ sel.zh }}</div>
      <div class="sp-hits" v-if="sel.hits.length">
        <span class="chip" v-for="h in sel.hits" :key="h.en">📌 {{ h.en }} → {{ h.zh }}</span>
      </div>
      <div class="sp-actions">
        <button class="primary" style="padding:4px 10px" @click="doTranslateSel">{{ sel.zh ? '重译' : '翻译' }}</button>
        <button style="padding:4px 10px" @click="pinSel">钉在页边</button>
        <button style="padding:4px 10px" @click="sendToGlossary">收进术语</button>
        <button style="padding:4px 10px" @click="askAboutSel">提问</button>
        <button class="ghost" style="padding:4px 8px" @click="sel.visible = false">×</button>
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
      <div v-if="vis.busy" class="vp-state">看图作答中…</div>
      <div v-if="vis.err" class="vp-state err">{{ vis.err }}</div>
      <MdLite v-if="vis.answer" class="vp-answer" :text="vis.answer" />
      <!-- 关掉的出口只有右上角那个 ×（和 Esc）：左下角再挂一个"关闭"是重复，
           底部只留真正要做的动作 -->
      <div class="sp-actions" v-if="vis.answer">
        <button class="primary" style="padding:4px 10px" @click="pinVisual">钉在页边</button>
      </div>
    </div>
    </Transition>

    <!-- 角色卡：点页边书签打开 -->
    <Transition name="pop">
    <div class="role-card" v-if="roleCard && roleCardData" ref="roleCardEl"
         :style="{ left: roleCard.x + 'px', top: roleCard.y + 'px' }"
         v-drag="{ onStart: () => (roleCardDragged = true) }" @mousedown.stop>
      <div class="rc-top" data-drag>
        <span class="rc-role" :style="{ color: ROLE_TEXT_COLOR[roleCardData.role] }">{{ ROLE_ZH[roleCardData.role] }}</span>
        <span v-if="roleCardData.anno.user_override" class="rc-flag">已改判</span>
        <span class="rc-num">¶{{ roleCard.idx }} · 第 {{ roleCardData.p.page + 1 }} 页</span>
        <button class="rc-x" title="关闭（Esc）" @click="closeRoleCard">×</button>
      </div>
      <div class="rc-purpose">{{ roleCardData.anno.purpose || '推断中' }}</div>
      <select :value="roleCardData.role" @change="e => $emit('override', { idx: roleCard.idx, role: e.target.value })">
        <option value="">回到推断</option>
        <option v-for="(zh, k) in ROLE_ZH" :key="k" :value="k">{{ zh }}</option>
      </select>
    </div>
    </Transition>

    <Transition name="pop">
      <button class="back-chip desk-float" v-if="backChip" :style="{ left: midX + 'px' }" @click="jumpBack">
        返回原位 · Alt+←
      </button>
    </Transition>
  </div>
</template>
