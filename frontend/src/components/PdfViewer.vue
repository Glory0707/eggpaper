<script>
/* 解析好的 PDF 文档缓存是**文件级**的：多窗格/重挂载共享同一份解析结果（key = pid:variant），
   第二次打开秒出。LRU 上限 6 份，超出淘汰最久未用的一份。 */
const docCache = new Map()
</script>

<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, jumpPara, askNotePrefill, paraByIdx, bandOf, kindColor, kindText, kindZH } from '../store'
import { lsGet, lsSet } from '../ls'
import { copyWithToast } from '../clip'

const props = defineProps({ pid: { type: String, required: true } })
/* 篇级数据自持：多窗格下每个实例只读自己这篇的段落/角色/批注/元信息，
   与活动篇的全局 store（右栏/顶栏用）井水不犯河水。 */
const paperMeta = ref(null)
const paras = ref([])
const annos = ref({})
const mnotes = ref([])
let savedScroll = null
async function loadPaperData() {
  const [pm, ps, a, m] = await Promise.all([
    api.paper(props.pid), api.paragraphs(props.pid),
    api.analysis(props.pid).catch(() => ({})), api.marginalia(props.pid).catch(() => ({})),
  ])
  paperMeta.value = pm
  paras.value = ps || []
  annos.value = a.annotations || {}
  mnotes.value = m.notes || []
}
import { lineSpanOf, findQuoteRects, findAllRects, clearTextIndex, sentenceAround } from '../find'
import { prettyChem } from '../chem'
import { translateStream } from '../api'
import MdLite from './MdLite.vue'
import EggMark from './EggMark.vue'
import { vDrag } from '../drag'
import { t, isEn } from '../i18n'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

const GUTTER_FULL = 184   // 页边批注带（184 宽 + 12 的左边距）

const deskEl = ref(null)
const ready = ref(false)
const loadPct = ref(0)         // 破壳进度：喂给入场那条细线
let creep = 0                   // 缓慢逼近的底速（本地 PDF 的下载是瞬时的，
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
const deskW = ref(0)             // 书桌宽度：浮条 max-width 钳在窗格里（多窗格窄格不横穿邻格）
const floatMax = computed(() => (deskW.value ? Math.max(220, deskW.value - 12) + 'px' : null))
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
const visInputEl = ref(null)

const canvases = ref([]), textLayers = ref([]), pageEls = ref([])
const doneKeys = new Set()
const renderQueues = new Map()   // gi -> 渲染链尾：同一画布严格串行，杜绝并发 render
let passToken = 0
const scroller = () => deskEl.value?.closest('.desk') || deskEl.value
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
  for (const p of paras.value) (m[p.page] ||= []).push(p)
  return m
})
/* 页边摆哪些批注。读者自己钉的（查译/框选答疑/自己写的批注）是**读者资产**：
   「AI 眉批」图层开关和档位开关管不着它们，唯一的开关是设置里的「我的钉卡」——
   默认显示，没有 AI 眉批也照常显示（用户原话），想收起去设置里点一下。
   AI 眉批四档就是纸上那四种笔触，一档一个开关（右栏「问题」页的眉批块里点）——
   批注上到三四十条时，"只看要当心"是读者的第一个念头。 */
const USER_KINDS = new Set(['lookup', 'region', 'note'])
const notesShown = computed(() => {
  const on = store.viewer.noteBands || {}
  const mineOn = store.viewer.layers.mine !== false
  return mnotes.value.filter(n =>
    (USER_KINDS.has(n.kind) && mineOn) ||
    (store.viewer.layers.marginalia && !USER_KINDS.has(n.kind) && on[bandOf(n)] !== false))
})
/* 页边要多宽，取决于"上面真有东西要放吗"：有批注 → 整条 184，没有 → 一点都不留，
   论文因此能多出近 200px 的宽度。这条宽度是 measure() 的输入，图层一变就得重排。 */
const gutterW = computed(() => (notesShown.value.length ? GUTTER_FULL : 0))
const gutterPad = computed(() => (gutterW.value ? 12 : 0))
const flatItems = computed(() => sheets.value.flatMap(s => s.items))

const pingId = ref(null)                  // 刚从纸上点回来的那条批注（亮一下）

/* ---------------- 文档装载与 sheets 构建 ---------------- */

async function getDoc(kind) {
  const key = `${props.pid}:${kind}`
  const hit = docCache.get(key)
  if (hit) { hit.at = Date.now(); return hit.doc }
  const task = pdfjsLib.getDocument(`/api/papers/${props.pid}/pdf?variant=${kind}`)
  if (!sheets.value.length) {
    task.onProgress = ({ loaded, total }) => {
      if (total) loadPct.value = Math.max(loadPct.value, Math.min(0.94, loaded / total))
    }
  }
  const doc = await task.promise
  docCache.set(key, { doc, at: Date.now() })
  while (docCache.size > 6) {              // LRU：最多留 6 份解析好的文档
    let oldest = null, ot = Infinity
    for (const [k, e] of docCache) if (e.at < ot) { ot = e.at; oldest = k }
    const e = docCache.get(oldest)
    try { e.doc.destroy() } catch { /* */ }
    docCache.delete(oldest)
  }
  return doc
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
      push([{ key: `m${i}`, doc: 'mono', page: i, origPage: -1, w: m.w, h: m.h, margin: false, text: true }])
  } else if (v === 'dual') {
    const td = await getDoc('dual'), tm = await meta(td)
    if (store.viewer.spread === 'spread') {
      const od = await getDoc('orig'), om = await meta(od)
      for (let i = 0; i < om.count && 2 * i + 1 < tm.count; i++)
        push([
          { key: `sl${i}`, doc: 'orig', page: i, origPage: i, w: om.w, h: om.h, margin: false, text: true },
          { key: `sr${i}`, doc: 'dual', page: 2 * i + 1, origPage: -1, w: tm.w, h: tm.h, margin: false, text: true },
        ])
    } else {
      for (let j = 0; j < tm.count; j++) {
        const isOrig = j % 2 === 0
        push([{ key: `di${j}`, doc: 'dual', page: j, origPage: isOrig ? j / 2 : -1, w: tm.w, h: tm.h, margin: isOrig, text: true }])
      }
    }
  }
  sheets.value = rows
}

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
  const anchor = keepPlace || !veryFirst ? currentAnchor() : null
  sheets.value = []
  doneKeys.clear()
  try {
    await buildSheets()
  } catch (e) {
    if (store.viewer.variant !== 'original') {
      const was = store.viewer.variant
      store.viewer.variant = 'original'      // 赋值会触发 watch → 重新 load
      toast(t((was === 'mono' ? '译文版' : '双语版') + '打不开，已切回原文；想再看可重新「全文翻译」'), 5000)
      loading = false
      return
    }
    toast(t('文档加载失败：{m}', { m: e.message }))
    ready.value = true
    loading = false
    return
  }
  try {
    await measure()
    await renderAll()
  } catch (e) {
    console.error('[eggpaper] 渲染失败：', e)
    toast(t('这份文档渲染失败了：{m}', { m: String(e.message || e).slice(0, 120) }), 6000)
    stopCreep()
    loadPct.value = 1
    ready.value = true
    loading = false
    return
  }
  stopCreep()
  loadPct.value = 1
  ready.value = true
  await nextTick()
  await measureNotes()
  if (anchor) applyAnchor(anchor)
  else if (savedScroll != null) { scroller().scrollTop = savedScroll }
  updateProg()
  if (veryFirst) setTimeout(onScroll, 800)
  if (pendingFind) {                      // 「文中」等在切回原文之后的那一次搜索
    const q = pendingFind
    pendingFind = null
    nextTick(() => { searchQ.value = q; searchOpen.value = true })
  }
  if (pendingJump) {                      // 渲染没赶上的那次跳转（跨篇《标题》¶n 常见）
    if (store.jump === pendingJump && Date.now() - pendingJump.at < 8000) applyJump()
    pendingJump = null
  }
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
  fitScale.value = (sc.clientWidth - 60 - gutter - (perRow === 2 ? 20 : 0)) / (first.w * perRow)
  pageFitScale.value = (sc.clientHeight - 56) / first.h
}

function updateMid() {
  const el = deskEl.value?.closest('.desk') || deskEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  midX.value = Math.round(r.left + r.width / 2)
  deskW.value = Math.round(r.width)
  deskRightX.value = Math.round(r.right - 3)
}

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
  if (seq === passToken) {
    rendering.value = false
    await measureNotes()
    if (searchOpen.value && searchQ.value.trim().length >= 2) runSearch()
  }
}

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
        clearTextIndex(tlEl)
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
        cands.push({ n: { id: 'pending', kind: 'lookup', page: it.origPage, quote: (p.text || '').slice(0, 60), note: t('翻译中…') }, anchor: p.bbox.y0 * scale.value, pending: true })
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
function isRegion(n) { return n.kind === 'region' || (n.quote || '').startsWith('[选区]') }
function regionBox(n) {
  const s = scale.value
  return n.rect ? [{ x: n.rect.x0 * s, y: n.rect.y0 * s, w: (n.rect.x1 - n.rect.x0) * s, h: (n.rect.y1 - n.rect.y0) * s }] : []
}

const quoteMarks = ref({})          // noteId -> [{x,y,w,h}]
const quoteCov = ref({})            // noteId -> 0~1：引文有多少能对回原文
function computeQuoteMarks() {
  const out = {}, cov = {}
  for (const n of notesShown.value) {
    if (isRegion(n) || !n.quote) continue
    const it = pageItem(n.page)
    const el = it && pageEls.value[it.gi]
    if (!el) continue
    const p = paraByIdx.value[n.para_idx]
    const box = p ? { y0: p.bbox.y0 * scale.value, y1: p.bbox.y1 * scale.value } : null
    const r = findQuoteRects(el, anchorText(n), box)
    if (r) { out[n.id] = r.rects; cov[n.id] = r.覆盖比 }
  }
  quoteMarks.value = out
  quoteCov.value = cov
}
function quoteLoose(n) {
  const c = quoteCov.value[n.id]
  return c != null && c < 0.75
}

const hotNote = ref(null)
function hoverNote(id) { hotNote.value = id }

/* 批注的标签：九种常用款用它自己的名字；模型自造的类型（kind='custom'）用模型起的标签；
   你自己钉的那些前面加"你 · "——一眼分得清哪句是别人说的、哪句是你自己写的。 */
function kindLabel(n) {
  const zh = kindZH(n)
  return USER_KINDS.has(n.kind) && zh ? t('你 · ') + zh : zh
}

/* 引文默认只看开头，想看全句点「全句」。
   不硬切：切口带省略号，而且有明确的展开出口——页边只有 154px 宽，
   一条 200 字的引文全铺出来会把整页的批注挤下去。 */
const openQuote = ref(null)
function toggleQuote(n) {
  openQuote.value = openQuote.value === n.id ? null : n.id
  measureNotes()
  setTimeout(measureNotes, 280)
}
function quoteShown(n) {
  const q = anchorText(n)
  return openQuote.value === n.id || q.length <= 44 ? q : q.slice(0, 44) + '…'
}
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
  const pid = props.pid
  pendingPara.value = idx
  let zh = ''
  try {
    await translateStream(props.pid, 'para', { idx }, ev => {
      if (ev.type === 'delta') zh += ev.text
      else if (ev.type === 'error') throw new Error(ev.message)
    }).done
    if (!zh.trim()) { toast(t('模型没返回内容，再试一次')); return }
    const p = paraByIdx.value[idx]
    await api.pin(pid, { quote: (p?.text || '').slice(0, 150), note: zh, para_idx: idx, page: p?.page ?? 0 })
    await refreshM()
  } catch (e) { toast(t('翻译失败：{m}', { m: e.message })) }
  finally { pendingPara.value = null }
}

/* ---------------- 划词 ---------------- */

const sel = reactive({ visible: false, x: 0, y: 0, text: '', context: '', paraIdx: 0, page: 0, zh: '', hits: [], busy: false, err: '' })
const selEl = ref(null)
const visEl = ref(null)
/* 气泡是流式长高的（译文一段段写进来、答案一行行出来），开的时候按 230/340px 预留的位置
   在内容长成后会探出窗口底——fixed 定位滚不到，只能把气泡挪上来。内容每次变化后按
   真实尺寸再钳一次；拖动过的位置也照钳，只是保证不出屏。钳位 watch 挂在 vis 声明之后
   （三个 reactive 都得已存在）。 */
function refitPop(el, pos) {
  if (!el || !pos?.visible) return
  const h = el.offsetHeight, w = el.offsetWidth
  if (!h) return
  const y = Math.max(8, Math.min(pos.y, window.innerHeight - h - 8))
  const x = Math.max(8, Math.min(pos.x, window.innerWidth - w - 8))
  pos.y = y; pos.x = x
}

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
  let context = '', paraIdx = -1, page = it ? (it.origPage >= 0 ? it.origPage : origPageOf(it)) : 0
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
  if (!notesShown.value.length) return
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
    await translateStream(props.pid, 'selection', { text: sel.text, context: sel.context }, ev => {
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
  await api.pin(props.pid, { quote: sel.text.slice(0, 150), note: sel.zh, para_idx: sel.paraIdx, page: sel.page })
  await refreshM()
  closeSel()
  toast(t('已钉在页边'))
}

async function copySel() {
  await copyWithToast(sel.zh || '', t('译文已复制'))
}
function sendToGlossary() {
  store.glossaryPrefill = { term_en: sel.text.slice(0, 80), term_zh: (sel.zh || '').replace('〔演示译文〕', '').slice(0, 24) }
  closeSel()
  toast(t('已带到术语表，请确认中文译法'))
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
  nextTick(() => mineEl.value?.focus({ preventScroll: true }))
}
async function saveMine() {
  const t = mine.text.trim()
  if (!t) return
  await api.pin(props.pid, { quote: sel.text.slice(0, 150), note: t, para_idx: sel.paraIdx,
                                   page: sel.page, kind: 'note' })
  mine.open = false; mine.text = ''
  await refreshM()
  closeSel()
  toast(t('已写在页边'))
}

async function refreshM() {
  const m = await api.marginalia(props.pid)
  mnotes.value = m.notes || []
  if (props.pid === store.currentId) Object.assign(store.marginalia, m, { pid: props.pid })
}

async function unpin(mid) {
  await api.unpin(props.pid, mid)
  await refreshM()
}

/* ---------------- 阅读器基本功能：缩放 / 页码 / 搜索 ---------------- */

function setFit(m) { fit.value = m; zoom.value = 1; saveLater() }
function stepZoom(d) {
  const next = scale.value * (d > 0 ? 1.15 : 1 / 1.15)
  fit.value = 'none'
  zoom.value = Math.min(3, Math.max(0.2, next))
  saveLater()
}
function commitPage() {
  const n = Number(pageIn.value)
  if (Number.isFinite(n) && n >= 1) gotoPage(n)
  else pageIn.value = String(pageNum.value)
}
function gotoPage(p) {
  const pno = Math.min(paperMeta.value?.n_pages || 1, Math.max(1, Math.round(p)))
  pageIn.value = String(pno)
  const it = pageItem(pno - 1)
  const el = it && pageEls.value[it.gi]
  if (!el) return
  backStack.push(scroller().scrollTop)
  scrollToY(el.offsetTop - 8, true)
  if (sel.visible) closeSel()
  if (vis.visible) closeVis()
  pageNum.value = pno
  backChip.value = true
  clearTimeout(applyJump._t)
  applyJump._t = setTimeout(() => (backChip.value = false), 3000)
}
function stepPage(d) { gotoPage(pageNum.value + d) }

function scrollToY(y, instant = false) {
  const sc = scroller()
  if (!sc) return
  const far = Math.abs(y - sc.scrollTop) > sc.clientHeight * 1.5
  sc.scrollTo({ top: y, behavior: instant || far ? 'auto' : 'smooth' })
}

async function runSearch() {
  const q = searchQ.value.trim()
  if (q.length < 2) { searchHits.value = []; searchAt.value = -1; lastSearchQ = ''; return }
  searchBusy.value = true
  const prev = lastSearchQ === q && searchAt.value >= 0 ? searchHits.value[searchAt.value] : null
  const hits = []
  for (const it of flatItems.value) {
    if (it.origPage < 0) continue
    const el = pageEls.value[it.gi]
    if (!el?.querySelector('.textLayer')) continue
    for (const h of findAllRects(el, q)) hits.push({ ...h, page: it.origPage, gi: it.gi, k: scale.value })
  }
  hits.sort((a, b) => (a.page - b.page) || (a.y - b.y))
  searchHits.value = hits
  lastSearchQ = q
  let at = -1
  if (hits.length) {
    at = 0
    if (prev) {
      const oldY = prev.rects[0].y * (scale.value / prev.k)
      let best = Infinity
      for (let i = 0; i < hits.length; i++) {
        const d = Math.abs(hits[i].page - prev.page) * 1e4 + Math.abs(hits[i].rects[0].y - oldY)
        if (d < best) { best = d; at = i }
      }
    }
  }
  searchAt.value = at
  searchBusy.value = false
  if (hits.length) gotoHit(at)
}
let lastSearchQ = ''
function gotoHit(i) {
  const hits = searchHits.value
  if (!hits.length) return
  searchAt.value = (i + hits.length) % hits.length
  const h = hits[searchAt.value]
  const it = pageItem(h.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return
  scrollToY(el.offsetTop + h.rects[0].y * hitK(h) - scroller().clientHeight * 0.3, true)
}
function searchStep(d) { if (searchHits.value.length) gotoHit(searchAt.value + d) }
function closeSearch() { searchOpen.value = false; searchQ.value = ''; searchHits.value = []; searchAt.value = -1; lastSearchQ = '' }
/* 命中框的实时比例：框是按测量那一刻的缩放（h.k）量的，页面现在活在 scale 档。
   缩放后的整轮重渲染要好几秒（大 PDF 更久），期间文字层已经是新档、框还是旧档
   的——按比例一换算，框就一直钉在词上，不用等渲染收尾的重跑（同一布局纯放大
   缩小，坐标严格按比例走，这个换算是精确的）。 */
function hitK(h) { return scale.value / (h.k || scale.value) }

function translateSelectionKey() {
  const s = window.getSelection()
  if (s && !s.isCollapsed && s.toString().trim()) onMouseUp({})
  else toast(t('先划选一段原文'))
}

/* ---------------- 跳转栈 / 键盘 API ---------------- */

const backStack = []

let pendingJump = null            // 跳转来时这篇还没渲染好（甚至还没挂载）：先记下，渲染收尾再补跳
function retryJump() {
  clearTimeout(retryJump._t)
  retryJump._t = setTimeout(() => {
    if (!pendingJump) return
    if (store.jump !== pendingJump || Date.now() - pendingJump.at > 8000) { pendingJump = null; return }
    applyJump()                    // 还没渲染完就再存再排；渲染好了就地跳
  }, 400)
}
{ /* 《别篇》¶n 点过来：跳转常发生在本组件挂载之前，watch 根本没赶上，挂载时先认领 */
  const j = store.jump
  if (j && (!j.pid || j.pid === props.pid) && Date.now() - j.at < 8000) { pendingJump = j; retryJump() }
}

async function applyJump() {
  const j = store.jump
  if (!j) return
  if (j.pid && j.pid !== props.pid) return
  if (!deskEl.value) { pendingJump = j; retryJump(); return }   // 纸面还没长出来：稍后补跳
  await nextTick()
  const it = pageItem(j.page)
  const el = it && pageEls.value[it.gi]
  if (!el) { pendingJump = j; retryJump(); return }   // 整篇还在出图：稍后补跳（同查找的 pendingFind）
  pendingJump = null
  backStack.push(scroller().scrollTop)
  if (backStack.length > 30) backStack.shift()
  scrollToY(el.offsetTop + j.y0 * scale.value - scroller().clientHeight * 0.28)
  flash.value = null
  if (j.rects?.length) {
    flash.value = { gi: it.gi, boxes: j.rects }
  } else {
    for (const p of parasByPage.value[j.page] || []) {
      const y0 = (p.lines?.[0]?.bbox.y0 ?? p.bbox.y0) * scale.value
      const y1 = (p.lines?.[p.lines.length - 1]?.bbox.y1 ?? p.bbox.y1) * scale.value
      if (y0 <= j.y0 * scale.value + 6 && y1 >= j.y0 * scale.value - 6) { flash.value = { idx: p.idx, gi: it.gi }; break }
    }
  }
  setTimeout(() => (flash.value = null), 2400)     // 2400 > 入场 180 + 停留 1600 + 淡出 520
  backChip.value = true
  clearTimeout(applyJump._t)
  applyJump._t = setTimeout(() => (backChip.value = false), 3000)
}

/* 眉批卡片上的引文：点一下跳到纸上那句话，位置就是划线的位置 */
function jumpQuote(n) {
  const boxes = markBoxes(n)
  const it = pageItem(n.page)
  if (!it) return
  const y = boxes.length ? boxes[0].y / scale.value : (n.rect?.y0 ?? paraByIdx.value[n.para_idx]?.bbox.y0 ?? 0)
  store.jump = { pid: props.pid, page: n.page, y0: y, y1: y + 1, rects: boxes, at: Date.now() }
}

function jumpBack() {
  const t = backStack.pop()
  if (t == null) return
  scroller()?.scrollTo({ top: t, behavior: 'smooth' })   // 卸载后迟到的调用：没有滚动容器就作罢
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
  if (!paras.value.length) return
  const list = paras.value
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
  else toast(t('先滚动到要译的段落'))
}

/* ---------------- 截图：拖拽框选，选区直接成图（Windows 截图的手感） ----------------
   进入模式后盖全屏暗罩、十字光标；松手先撤罩、等屏幕恢复原样这一拍再抓窗口
   物理像素（后端按前端量好的镶边裁成纯视口），前端按选区 × devicePixelRatio
   裁剪——所见即所得，缩放屏上天然比显示分辨率高一档。剪贴板是底线，落盘看设置。 */
const shotMode = ref(false)
const shotSel = ref(null)
const shotBusy = ref(false)
function startShot() {
  if (shotBusy.value || shotMode.value) return
  shotMode.value = true
  window.addEventListener('keydown', shotEsc, true)
}
function stopShot() {
  if (!shotMode.value) return
  shotMode.value = false
  shotSel.value = null
  window.removeEventListener('keydown', shotEsc, true)
}
function shotEsc(e) {
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); stopShot() }
}
function shotDown(e) {
  if (e.button !== 0) return
  e.preventDefault()
  const p0 = { x: e.clientX, y: e.clientY }
  shotSel.value = { x0: p0.x, y0: p0.y, x1: p0.x, y1: p0.y }
  const move = ev => { shotSel.value = { x0: p0.x, y0: p0.y, x1: ev.clientX, y1: ev.clientY } }
  const cancel = () => stopShot()
  const up = ev => {
    document.removeEventListener('mousemove', move)
    document.removeEventListener('mouseup', up)
    window.removeEventListener('blur', cancel)
    const s = shotSel.value
    shotSel.value = null
    if (!s) return
    const x0 = Math.min(p0.x, ev.clientX), y0 = Math.min(p0.y, ev.clientY)
    const x1 = Math.max(p0.x, ev.clientX), y1 = Math.max(p0.y, ev.clientY)
    if (x1 - x0 < 8 || y1 - y0 < 8) return     // 点一下不算：留在模式里继续拖
    shotMode.value = false                     // 撤罩先——抓的是没有暗罩的原画面
    window.removeEventListener('keydown', shotEsc, true)
    finishShot({ x0, y0, x1, y1 })
  }
  document.addEventListener('mousemove', move)
  document.addEventListener('mouseup', up)
  window.addEventListener('blur', cancel)
}
async function finishShot(sel) {
  if (shotBusy.value) return
  shotBusy.value = true
  await new Promise(r => setTimeout(r, 150))   // 等暗罩从屏幕上消失这一拍
  try {
    const r = await api.screenshot({
      chrome_top: window.outerHeight - window.innerHeight,
      border: (window.outerWidth - window.innerWidth) / 2,
      dpr: window.devicePixelRatio,
    })
    const img = new Image()
    img.src = 'data:image/png;base64,' + r.png
    await img.decode()
    const d = window.devicePixelRatio || 1
    const sw = Math.max(1, Math.round((sel.x1 - sel.x0) * d))
    const sh = Math.max(1, Math.round((sel.y1 - sel.y0) * d))
    const cv = document.createElement('canvas')
    cv.width = sw; cv.height = sh
    cv.getContext('2d').drawImage(img, Math.round(sel.x0 * d), Math.round(sel.y0 * d), sw, sh, 0, 0, sw, sh)
    const blob = await new Promise(res => cv.toBlob(res, 'image/png'))
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    let name = null
    if (store.settings?.shot_save !== false) {
      const b64 = await new Promise(res => {
        const fr = new FileReader()
        fr.onload = () => res(String(fr.result).split(',')[1])
        fr.readAsDataURL(blob)
      })
      name = (await api.screenshotSave(paperMeta.value?.title || paperMeta.value?.filename || '', pageNum.value, b64)).name
    }
    toast(name ? t('已复制 · 已保存 {n}', { n: name }) : t('已复制到剪贴板'))
  } catch (e) {
    toast(t('截图失败：{m}', { m: e.message }))
  } finally { shotBusy.value = false }
}

const viewerApiObj = { step, translateCurrent, startShot, jumpBack, translateSelectionKey, stepPage, gotoPage, openSearch, findInPaper }
watch(() => props.pid === store.currentId, act => { if (act) store.viewerApi = viewerApiObj }, { immediate: true })

function openSearch() { searchOpen.value = true }
/* 别的面板（术语表）说"去原文里找这个词"：预填 + 打开 + 自动搜（searchQ 的 watch 会跑）。
   术语和原文本来是两个各自翻的地方，连起来之后"这个词在这篇里怎么用的"才问得出口。 */
let pendingFind = null       // 等这一轮渲染完再搜（换模式要重新出图、建文字层）
function findInPaper(q) {
  if (!q) return
  if (store.viewer.variant !== 'original') {
    store.viewer.variant = 'original'
    toast(t('已切回原文再找'))
    pendingFind = q          // 换模式要重新出图、重建文字层：等 load() 收尾再搜
    return
  }
  if (store.viewer.frame) {
    store.viewer.frame = false        // 文字层是 v-if 挂的：退出框选这一拍就回来了
    nextTick(() => { searchQ.value = q; searchOpen.value = true; runSearch() })
    return
  }
  searchQ.value = q
  searchOpen.value = true
}

/* ---------------- 滚动：scroll-spy + 位置记忆 ---------------- */

let spyT = null, saveT = null
function onScroll() {
  updateProg()
  const sc = scroller()
  if (!sc) return          // 卸载后残留的定时器（setTimeout(onScroll, 800) / spyT）还会打一枪
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
    for (const p of paras.value) {
      const y = paraAbsY(p.idx)
      if (y == null) continue
      const d = Math.abs(y - focusY)
      if (d < bestD) { bestD = d; best = p.idx }
    }
    if (props.pid === store.currentId) store.readingPara = best
    clearTimeout(saveT)
    saveT = setTimeout(savePos, 600)
  }, 220)
}

function savePos() {
  if (!scroller()) return
  lsSet('pos:' + props.pid, {
    scroll: scroller().scrollTop, variant: store.viewer.variant,
    spread: store.viewer.spread, zoom: zoom.value, fit: fit.value,
  })
}
function saveLater() { clearTimeout(saveT); saveT = setTimeout(savePos, 250) }

onMounted(async () => {
  loadPaperData().then(() => {
    if (mnotes.value.length) {
      freshNotes.value = true
      setTimeout(() => (freshNotes.value = false), 1800)
    }
  }).catch(() => {})
  const saved = lsGet('pos:' + props.pid, {})
  savedScroll = saved.scroll ?? null
  if (saved.fit) fit.value = saved.fit
  if (saved.zoom) zoom.value = saved.zoom
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
  if (store.viewerApi === viewerApiObj) store.viewerApi = null   // 活动窗格卸载了，快捷键别再打进来
  selStream?.abort()
  clearTimeout(spyT); clearTimeout(saveT); clearTimeout(scheduleRender._t)
  clearTimeout(focusNote._t); clearTimeout(applyJump._t)
  ro?.disconnect()
  document.removeEventListener('mouseup', onMouseUp)
  document.removeEventListener('mousedown', onDocDown)
  window.removeEventListener('resize', updateMid)
  scroller()?.removeEventListener('scroll', onScroll)
  scroller()?.removeEventListener('wheel', onUserScroll)
  scroller()?.removeEventListener('wheel', onWheelZoom)
  scroller()?.removeEventListener('touchstart', onUserScroll)
  stopCreep()
  loadPct.value = 1
  ready.value = true
})

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

/* 气泡长高不出屏的钳位（见 refitPop 注释）。写在三个 reactive 都声明之后。 */
watch([() => sel.visible, () => sel.zh, () => sel.busy, () => sel.err, () => mine.open],
  () => nextTick(() => refitPop(selEl.value, sel)))
watch([() => vis.visible, () => vis.answer, () => vis.busy, () => vis.err],
  () => nextTick(() => refitPop(visEl.value, vis)))

function startFrameDrag(e, it) {
  if (!store.viewer.frame || e.button !== 0) return
  if (it.origPage < 0) { toast(t('译文页不能框选，切回「原文」再圈')); return }
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
    const s = scale.value
    let paraIdx = 0
    for (const p of parasByPage.value[it.origPage] || []) {
      if (y0 / s >= p.bbox.y0 - 3 && y0 / s <= p.bbox.y1 + 3) { paraIdx = p.idx; break }
    }
    Object.assign(vis, { visible: true, x: Math.min(window.innerWidth - 410, ev.clientX + 12),
                         y: Math.min(window.innerHeight - 340, Math.max(64, ev.clientY - 60)),
                         img, question: t('解释选区里的内容。'), answer: '', busy: false, err: '',
                         page: it.origPage, paraIdx,
                         rect: it.origPage >= 0 ? { x0: x0 / s, y0: y0 / s, x1: x1 / s, y1: y1 / s } : null })
  }
  frameRect.value = { x0: p0.x, y0: p0.y, x1: p0.x, y1: p0.y }
  document.addEventListener('mousemove', move); document.addEventListener('mouseup', up)
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

/* 「分析此图/公式/表格」三个按钮只预填不发：用户多半要改两句再问（Enter 发送）。
   填完把光标挪到输入框末尾——改提示词从那里开始。 */
function visFill(q) {
  vis.question = q
  nextTick(() => {
    const el = visInputEl.value
    if (!el) return
    el.focus({ preventScroll: true })
    const n = el.value.length
    el.setSelectionRange(n, n)
  })
}

async function pinVisual() {
  if (vis.busy) return
  if (!vis.answer) await askVisual()
  if (!vis.answer) return
  await api.pin(props.pid, {
    quote: '[选区] ' + vis.question.slice(0, 60), note: vis.answer.slice(0, 560),
    para_idx: vis.paraIdx, page: vis.page, rect: vis.rect,
  })
  await refreshM()
  closeVis()
  toast(t('已钉在页边'))
}

watch(() => store.visPrefill, pf => {
  if (!pf) return
  Object.assign(vis, { visible: true, x: Math.max(60, Math.floor(midX.value || window.innerWidth / 2) - 190), y: 90,
                       img: pf.img, question: pf.question || t('讲解这张图。'), answer: '', busy: false, err: '',
                       page: pf.page ?? 0, paraIdx: pf.paraIdx ?? 0, rect: pf.rect ?? null })
  store.visPrefill = null
  askVisual()
})

/* 点空白处收起浮层；Esc 交给 store.escTick */
function onDocDown(e) {
  const t = e.target
  if (!t?.closest) return
  if (vis.visible && !t.closest('.vis-pop')) closeVis()
  if (sel.visible && !t.closest('.sel-pop')) closeSel()
}
watch(() => store.escTick, () => {
  closeSel()
  if (vis.visible) closeVis()
  if (searchOpen.value) closeSearch()
})
watch(searchOpen, v => { if (v) nextTick(() => searchInputEl.value?.focus({ preventScroll: true })) })
let searchT = null
watch(searchQ, () => { clearTimeout(searchT); searchT = setTimeout(runSearch, 320) })

watch(() => store.viewer.variant, () => { doneKeys.clear(); load({ keepPlace: true }); saveLater() })
watch(() => store.viewer.spread, () => { doneKeys.clear(); load({ keepPlace: true }); saveLater() })
watch(scale, () => { doneKeys.clear(); scheduleRender(); saveLater() })
watch(paras, () => { spanCache.clear() })
watch(() => store.jump, applyJump)
watch(() => store.viewer.layers, () => reflow(), { deep: true })
watch(() => store.reflowTick, () => reflow())
watch(() => store.viewer.noteBands, () => reflow(), { deep: true })
watch(mnotes, (n, o) => {
  spanCache.clear()
  if (n.length && (!o || n.length > o.length)) {
    freshNotes.value = true
    setTimeout(() => (freshNotes.value = false), 1600)
  }
  measureNotes()
})
/* 活动篇的眉批由 App 轮询进全局 store——是自己的就收下（生成中的实时刷新走这条） */
watch(store.marginalia, m => {
  if (m.pid === props.pid && m.notes) mnotes.value = m.notes
})
</script>

<template>
  <div class="desk-inner" ref="deskEl">
    <Transition name="desk" mode="out-in">
    <div class="hatch" v-if="!ready">
      <EggMark class="hatch-mark" />
      <div class="hatch-line"><i :style="{ width: Math.round(loadPct * 100) + '%' }" /></div>
      <div class="hatch-word">{{ t('正在破壳') }}</div>
    </div>

    <div v-else class="sheet-stage">
      <div class="spread-row" v-for="(s, si) in sheets" :key="si">
        <div class="page-wrap" v-for="it in s.items" :key="it.key">
            <div class="page" :ref="el => (pageEls[it.gi] = el)"
                 :data-page="it.origPage" :data-scale="scale"
                 :style="{ width: it.w * scale + 'px', height: it.h * scale + 'px' }"
                 @mousedown="e => startFrameDrag(e, it)">

            <canvas :ref="el => (canvases[it.gi] = el)"></canvas>
            <div class="care-wash" v-if="store.viewer.care !== 'off'"></div>
                        <div class="textLayer" v-if="it.text" :class="{ 'tl-off': store.viewer.frame }"
                 :ref="el => (textLayers[it.gi] = el)"></div>
            <div v-if="frameRect && frameRect.gi === it.gi" class="frame-rect"
                 :style="{ left: Math.min(frameRect.x0, frameRect.x1) + 'px', top: Math.min(frameRect.y0, frameRect.y1) + 'px',
                           width: Math.abs(frameRect.x1 - frameRect.x0) + 'px', height: Math.abs(frameRect.y1 - frameRect.y0) + 'px' }"></div>

            <div class="para-zone">
              <template v-for="(p, pi) in parasByPage[it.origPage] || []" :key="'f' + p.idx">
                <div v-if="flash?.idx === p.idx && flash?.gi === it.gi" class="para-fade hot" :style="rectStyle(p)"></div>
              </template>

                            <div v-for="(b, bi) in flash?.gi === it.gi ? flash.boxes || [] : []" :key="'fb' + bi"
                   class="para-fade hot boxed" :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              <template v-for="(h, hi) in searchHitsOnPage(it.origPage)" :key="'s' + hi">
                <div v-for="(b, bi) in h.rects" :key="bi" class="find-hit" :class="{ cur: h === currentHit }"
                     :style="{ left: b.x * hitK(h) + 'px', top: b.y * hitK(h) + 'px',
                               width: b.w * hitK(h) + 'px', height: b.h * hitK(h) + 'px' }"></div>
              </template>

                            <template v-for="{ n } in notesOnPage(it.origPage)" :key="'n' + n.id">
                <div v-for="(b, bi) in markBoxes(n)" :key="bi" class="mg-mark" :data-nid="n.id"
                     :class="['b-' + bandOf(n), { draw: freshNotes, hot: hotNote === n.id, loose: quoteLoose(n) }]"
                     :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              </template>
            </div>
          </div>

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
                  {{ pending ? t('翻译中') : kindLabel(n) }}
                </span>
                <span v-if="(n.note || '').length > 34" class="mg-more">{{ expandedNote === n.id ? t('收起') : t('展开') }}</span>
                                <button v-if="!pending" class="mg-ask" @click.stop="askNotePrefill(n, { openRail: true })">{{ t('问 ↗') }}</button>
                <button v-if="!pending" class="mg-del" @click.stop="unpin(n.id)">×</button>
              </div>
              <div class="mg-body">{{ prettyChem(n.note) }}</div>
                            <div class="mg-quote-row">
                <span class="mg-quote" :class="{ all: openQuote === n.id }"
                      :title="t('跳到纸上这句：{q}', { q: anchorText(n) })" @click.stop="jumpQuote(n)">“{{ quoteShown(n) }}”</span>
                <button v-if="anchorText(n).length > 44" class="mg-qall" @click.stop="toggleQuote(n)">
                  {{ openQuote === n.id ? t('收起') : t('全句') }}
                </button>
                                <span v-if="quoteLoose(n)" class="mg-loose" :title="t('引文与原文略有出入')">≈</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
    </Transition>

        <div class="stage-line" v-if="ready && rendering"><i /></div>

        <div class="read-prog" v-if="ready" :style="{ left: deskRightX + 'px' }"><i :style="{ height: progPct * 100 + '%' }" /></div>

        <Transition name="fade">
    <div v-if="ready" class="desk-float zoom-bar" :style="{ left: midX + 'px', maxWidth: floatMax }"
         v-drag="{ key: 'zoombar' }" data-drag>
      <button :title="t('上一页（PageUp）')" @click="stepPage(-1)">‹</button>
      <span class="zb-page">
        <input ref="pageInputEl" v-model="pageIn" class="zb-input" :title="t('跳到第几页')"
               @keydown.enter="commitPage(); pageInputEl?.blur()" @blur="pageIn = String(pageNum)" />
        <em>/ {{ paperMeta?.n_pages || 0 }}</em>
      </span>
      <button :title="t('下一页（PageDown）')" @click="stepPage(1)">›</button>
      <span class="zb-sep"></span>
      <button :class="{ on: fit === 'width' }" @click="setFit('width')">{{ t('适宽') }}</button>
      <button :class="{ on: fit === 'page' }" @click="setFit('page')">{{ t('适页') }}</button>
      <button class="zb-num" :title="t('实际大小')" @click="setFit('none')">{{ zoomPct }}%</button>
      <button :title="t('缩小')" @click="stepZoom(-1)">－</button>
      <button :title="t('放大')" @click="stepZoom(1)">＋</button>
      <span class="zb-sep"></span>
      <button :title="t('查找（Ctrl+F）')" :class="{ on: searchOpen }" @click="searchOpen = !searchOpen">{{ t('查找') }}</button>
      <button :title="t('截图（复制到剪贴板）')" :class="{ on: shotMode }" :disabled="shotBusy" @click="startShot">{{ t('截图') }}</button>
    </div>
    </Transition>

        <Transition name="pop">
    <div v-if="ready && searchOpen" class="desk-float find-bar" :style="{ left: midX + 'px', maxWidth: floatMax }"
         v-drag="{ key: 'findbar' }" data-drag>
      <input ref="searchInputEl" v-model="searchQ" class="fb-input" :placeholder="t('在论文里找…')"
             @keydown.enter="searchStep(1)" @keydown.escape="closeSearch" />
      <span class="fb-count">
        {{ searchHits.length ? (searchAt + 1) + ' / ' + searchHits.length : (searchBusy ? '…' : t('无结果')) }}
      </span>
      <button :disabled="!searchHits.length" :title="t('上一个（Enter）')" @click="searchStep(-1)">‹</button>
      <button :disabled="!searchHits.length" :title="t('下一个（Enter）')" @click="searchStep(1)">›</button>
      <button class="ghost" :title="t('关闭（Esc）')" @click="closeSearch">×</button>
    </div>
    </Transition>

        <Transition name="pop">
    <div v-if="ready && store.viewer.frame" class="frame-hint desk-float" :style="{ left: midX + 'px', maxWidth: floatMax }">
      <button @click="store.viewer.frame = false">{{ t('退出框选') }}</button>
    </div>
    </Transition>

        <Transition name="pop">
    <div class="sel-pop" ref="selEl" v-if="sel.visible" :style="{ left: sel.x + 'px', top: sel.y + 'px' }"
         v-drag="{ key: 'selpop' }" data-drag @mouseup.stop>
      <div v-if="!sel.zh && !sel.busy && !sel.err" style="font-size:var(--fs-sm);color:var(--ink-3)">
        {{ t('已选 {n} 字符', { n: sel.text.length }) }}<span v-if="sel.paraIdx >= 0" class="mono-num"> · ¶{{ sel.paraIdx }}</span>
      </div>
      <div v-if="sel.busy && !sel.zh" style="font-size:var(--fs-sm);color:var(--ink-3)">{{ t('翻译中…') }}</div>
      <div v-if="sel.err && !sel.zh" style="font-size:var(--fs-sm);color:var(--vermilion)">{{ sel.err }}</div>
      <div class="sp-zh" v-if="sel.zh">{{ sel.zh }}<span v-if="sel.busy" class="qa-caret"></span></div>
      <div class="sp-hits" v-if="sel.hits.length">
        <span class="chip" v-for="h in sel.hits" :key="h.en">📌 {{ h.en }} → {{ h.zh }}</span>
      </div>
            <div class="sp-mine" v-if="mine.open">
        <textarea ref="mineEl" v-model="mine.text" rows="3" :placeholder="t('就这句写点什么…（Ctrl+Enter 保存）')"
                  @mouseup.stop @keydown.enter.ctrl="saveMine"></textarea>
      </div>
      <div class="sp-actions">
        <template v-if="mine.open">
          <button class="primary" style="padding:4px 10px" :disabled="!mine.text.trim()" @click="saveMine">{{ t('写到页边') }}</button>
          <button style="padding:4px 10px" @click="mine.open = false">{{ t('取消') }}</button>
        </template>
        <template v-else>
          <template v-if="!isEn()">
            <button class="primary" style="padding:4px 10px" @click="doTranslateSel" :disabled="sel.busy">
              {{ sel.busy ? t('翻译中') : (sel.zh ? t('重译') : t('翻译')) }}
            </button>
            <button v-if="sel.zh" style="padding:4px 10px"
                    @click="copySel">{{ t('复制译文') }}</button>
            <button style="padding:4px 10px" @click="pinSel">{{ t('钉在页边') }}</button>
            <button style="padding:4px 10px" @click="sendToGlossary">{{ t('收进术语') }}</button>
          </template>
          <button style="padding:4px 10px" @click="openMine">{{ t('写批注') }}</button>
          <button style="padding:4px 10px" @click="askAboutSel">{{ t('提问') }}</button>
          <button class="ghost" style="padding:4px 8px" @click="closeSel()">×</button>
        </template>
      </div>
    </div>
    </Transition>

            <Transition name="pop">
    <div class="sel-pop vis-pop" ref="visEl" v-if="vis.visible" :style="{ left: vis.x + 'px', top: vis.y + 'px' }"
         v-drag="{ key: 'vispop' }" data-drag @mouseup.stop>
      <div class="vp-head">
        <span class="mono-label">{{ t('选区问 AI') }}</span>
        <button class="vp-x" :title="t('关闭（Esc）')" @click="closeVis">×</button>
      </div>
      <img class="vis-img" :src="vis.img" />
      <input ref="visInputEl" type="text" v-model="vis.question" style="width:100%; margin-top:8px"
             @keydown.enter="askVisual" :placeholder="t('问这个选区…')" />
      <div class="sp-actions">
                <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="visFill(t('分析这张图：画了什么、支持什么结论'))">{{ t('分析此图') }}</button>
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="visFill(t('分析这个公式：每一步的含义和推导逻辑'))">{{ t('分析公式') }}</button>
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="visFill(t('分析这张表：趋势、异常和可疑之处'))">{{ t('分析表格') }}</button>
      </div>
      <div v-if="vis.busy" class="vp-state">{{ t('正在看图') }}</div>
      <div v-if="vis.err" class="vp-state err">{{ vis.err }}</div>
      <MdLite v-if="vis.answer" class="vp-answer" :text="vis.answer" />
            <div class="sp-actions" v-if="vis.answer">
        <button class="primary" style="padding:4px 10px" @click="pinVisual">{{ t('钉在页边') }}</button>
      </div>
    </div>
    </Transition>

    <Transition name="pop">
      <button class="back-chip desk-float" v-if="backChip" :style="{ left: midX + 'px', maxWidth: floatMax }" @click="jumpBack">
        {{ t('返回原位 · Alt+←') }}
      </button>
    </Transition>

    <div v-if="shotMode" class="shot-mask" :class="{ dim: !shotSel }"
         @contextmenu.prevent="stopShot" @wheel.prevent @mousedown="shotDown">
      <div class="shot-rect" v-if="shotSel"
           :style="{ left: Math.min(shotSel.x0, shotSel.x1) + 'px', top: Math.min(shotSel.y0, shotSel.y1) + 'px',
                     width: Math.abs(shotSel.x1 - shotSel.x0) + 'px', height: Math.abs(shotSel.y1 - shotSel.y0) + 'px' }"></div>
    </div>
  </div>
</template>
