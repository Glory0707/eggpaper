<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, paraByIdx, CORE_ROLES, bandOf, kindColor, kindText, kindZH } from '../store'
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
/* 页边摆哪些批注。读者自己钉的（查译/框选答疑/自己写的批注）是**读者资产**：
   「AI 眉批」图层开关和档位开关管不着它们，唯一的开关是设置里的「我的钉卡」——
   默认显示，没有 AI 眉批也照常显示（用户原话），想收起去设置里点一下。
   AI 眉批四档就是纸上那四种笔触，一档一个开关（右栏「问题」页的眉批块里点）——
   批注上到三四十条时，"只看要当心"是读者的第一个念头。 */
const USER_KINDS = new Set(['lookup', 'region', 'note'])
const notesShown = computed(() => {
  const on = store.viewer.noteBands || {}
  const mineOn = store.viewer.layers.mine !== false
  return store.marginalia.notes.filter(n =>
    (USER_KINDS.has(n.kind) && mineOn) ||
    (store.viewer.layers.marginalia && !USER_KINDS.has(n.kind) && on[bandOf(n)] !== false))
})
/* 页边要多宽，取决于"上面真有东西要放吗"：有批注 → 整条 184，没有 → 一点都不留，
   论文因此能多出近 200px 的宽度。这条宽度是 measure() 的输入，图层一变就得重排。 */
const gutterW = computed(() => (notesShown.value.length ? GUTTER_FULL : 0))
const gutterPad = computed(() => (gutterW.value ? 12 : 0))
const flatItems = computed(() => sheets.value.flatMap(s => s.items))

// 角色（8 类）现在只服务于一件事：略读时该把哪几段蒙掉。界面上不再有它的位置
function roleOf(p) {
  return store.analysis.annotations[String(p.idx)]?.role || null
}
function isCore(p) { return CORE_ROLES.includes(roleOf(p) || 'background') }

/* ---------------- 略读：把不用细读的段落整段变灰，读者要看得见、也要能自己扳 ----------------
   略读只做一件事：把"不用细读"的段落**整段**变灰（用户定的：大段大段地灰才算略读，
   一段中间突然留一句不灰反而迷乱）。三条规定：
   ① 位置正确——只灰**正文段落**（析读判成背景/样板/参考文献的段），图注表注、
      图片、首页一概不灰；参考文献既然不用看，就**整段全灰**（不打折）；
   ② 整段整段——按段为单位灰，不做句级切分；段落灰与不灰的边界就是段界；
   ③ 效果干净——没有"蒙版"：那一段的文字和符号**本体变成灰色**（纱与纸同色，只把
      下面的深色像素统一变浅，任何黑点都透不出来）；悬停掀开看一眼（字回黑），
      点一下是"这段我也要读"（按篇记在本地）。 */
const LS_KEEP = 'eggpaper:skimKeep:'
function loadKeep() {
  try { return new Set(JSON.parse(localStorage.getItem(LS_KEEP + store.currentId) || '[]')) } catch { return new Set() }
}
const skimKeep = ref(loadKeep())
const figs = ref([])                        // 这一篇的图片框（略读时避开它们）
async function loadFigs() {
  if (!store.currentId) return
  try { const r = await api.figures(store.currentId); figs.value = r.figures || [] }
  catch { figs.value = [] }
}
const pingId = ref(null)                  // 刚从纸上点回来的那条批注（亮一下）
const kept = idx => skimKeep.value.has(idx)
function toggleKeep(idx) {
  const s = new Set(skimKeep.value)
  if (s.has(idx)) s.delete(idx); else s.add(idx)
  skimKeep.value = s
  try { localStorage.setItem(LS_KEEP + store.currentId, JSON.stringify([...s])) } catch { /* 存不下就只管这一次会话 */ }
}
/* 点蒙纱 = "这段也要读"，但**拖选**（想复制那几个字）不该改状态：
   位移超过 4px、或者真的选出了东西，就当成一次没选上的选字，什么都不做。 */
const veilDown = ref(null)
function onVeilClick(e, idx) {
  const d = veilDown.value
  veilDown.value = null
  if (d && (Math.abs(e.clientX - d.x) > 4 || Math.abs(e.clientY - d.y) > 4)) return
  if (window.getSelection()?.isCollapsed === false) return
  toggleKeep(idx)
}
/* 有「值得读 / 要当心」批注、或读者自己钉过东西的段落不蒙。那儿已经插了话/钉了子，
   把整段蒙掉等于把读者自己的锚点一起藏起来。略读该略的是没有信息量的铺垫。 */
const protectedIdx = computed(() => {
  const s = new Set()
  // 用**全部**批注而不是过滤后的那一份：这条规则说的是"那段有东西要看"，
  // 跟用户此刻收起了哪一档（纯粹是显示偏好）无关
  for (const n of store.marginalia.notes) {
    const b = bandOf(n)
    if (b === 'good' || b === 'warn' || b === 'mine') s.add(n.para_idx)
  }
  return s
})
/* 图区（/figures 认出来的图片框）不蒙，图上的坐标轴文字、图注也不蒙——用户原话
   "不要在图片以及图片上的文字加蒙版"。两个坐标系都是 PDF 点，直接比。
   **必须带页号**：图片框只属于它那一页，拿别页的图框比坐标，正文行会因为
   "跟另一页的图同一位置"被整行丢掉（实测：第 2 页的句子撞上第 4 页的图框）。 */
function onFigure(pno, b) {
  return figs.value.some(f => f.page === pno && !(b.x1 <= f.x0 || b.x0 >= f.x1 || b.y1 <= f.y0 || b.y0 >= f.y1))
}
/* 该灰哪些段——段落级的判断写在一处。
   ① 候选只有两类：模型判成 background / boilerplate 的，以及参考文献段；
      图注/表注（解析器标了 caption）不是正文，永不入围；
   ② 首页不灰（标题、摘要、引言是"这篇讲什么"，灰掉它略读就没意义了）；
   ③ 有批注的段、读者手选的段不灰；
   ④ **不打折**：参考文献既然不用看就整个全灰，背景/样板段也是一段就整段灰——
      不设数量上限、不做句级切分（用户定的：大段大段地灰才算略读，
      一段中间突然留一句不灰反而迷乱）。 */
const skimSkip = computed(() => {
  if (!store.viewer.layers.skim) return new Set()
  // 综述的正文就是"梳理文献"本身——把 background/boilerplate 灰掉等于把血肉蒙掉
  // （实测一篇 GNN 综述 82% 正文会被判成背景）。综述只灰参考文献区，正文一律保留。
  const review = store.paper?.paper_type === 'review'
  return new Set(store.paras.filter(p => {
    if (p.page <= 0 || p.caption) return false
    if (review) return !!p.in_refs
    if (kept(p.idx) || protectedIdx.value.has(p.idx)) return false
    const r = roleOf(p)
    return !!p.in_refs || r === 'boilerplate' || r === 'background'
  }).map(p => p.idx))
})
function veiled(p, pno) { return pno > 0 && skimSkip.value.has(p.idx) }

/* 参考文献区多数根本不在段落流里：解析器会丢掉每条 <14 词的文献条目，
   甚至从 References 标题起整页跳过。段落流**之后**的那些页（参考文献/附录/
   补充材料——正是"不用看"的部分）整页变灰；不拦鼠标，想复制引文照样能选。 */
const lastParaPage = computed(() => store.paras.reduce((m, p) => Math.max(m, p.page), -1))

const veilRects = ref({})                 // origPage -> { paraIdx: [{x,y,w,h}] }
const veilPageRects = ref({})             // origPage -> [{x,y,w,h}]：整页置灰的那些页

/* 页级的文字行：不限段落，整页的 span 按 y 归组（首页页眉例外——首页不参与略读） */
function pageRows(el, s) {
  const base = el.getBoundingClientRect()
  const rows = new Map()
  for (const span of el.querySelectorAll('.textLayer span')) {
    const r = span.getBoundingClientRect()
    if (r.width < 1 || r.height < 1) continue
    const x = r.left - base.left, y = r.top - base.top
    if (y + r.height < 4 || y > base.height - 4) continue
    const k = Math.round(y)
    const cur = rows.get(k)
    if (cur) { cur.x0 = Math.min(cur.x0, x); cur.x1 = Math.max(cur.x1, x + r.width) }
    else rows.set(k, { x0: x, x1: x + r.width, y, h: r.height })
  }
  return [...rows.values()]
}

/* 段内的文字行（跨栏安全）：中心落在这一段框里的 span 按 y 归组合并成整行。
   蒙纱的段落模式和句子模式都用它。
   **一个 span 都不能丢**：宽度过滤会把"l"、"1"、"."这类窄字形 span 排掉，
   它们的墨迹没被灰到，就成了灰字里的黑点（用户报的"还有黑色噪点"）。
   行盒四周留 1–2px 余量：字形的墨迹常比 pdf.js 量的矩形宽出一丝（斜体、字肩），
   lighten 混合下余量本身不可见，只会把这点墨迹也一并变灰。 */
function paraRows(p, el, s) {
  const base = el.getBoundingClientRect()
  const bx0 = p.bbox.x0 * s - 2, bx1 = p.bbox.x1 * s + 2
  const by0 = p.bbox.y0 * s - 2, by1 = p.bbox.y1 * s + 2
  const rows = new Map()
  for (const span of el.querySelectorAll('.textLayer span')) {
    const r = span.getBoundingClientRect()
    if (r.width < 1 || r.height < 1) continue
    const x = r.left - base.left, y = r.top - base.top
    const cx = x + r.width / 2
    if (cx < bx0 || cx > bx1 || y + r.height < by0 || y > by1) continue
    const k = Math.round(y)
    const cur = rows.get(k)
    if (cur) { cur.x0 = Math.min(cur.x0, x); cur.x1 = Math.max(cur.x1, x + r.width) }
    else rows.set(k, { x0: x, x1: x + r.width, y, h: r.height })
  }
  return [...rows.values()]
}

function paraRowBoxes(p, el, s) {
  return paraRows(p, el, s)
    .filter(l => !onFigure(p.page, { x0: (l.x0 - 1) / s, x1: (l.x1 + 1) / s, y0: (l.y - 2.5) / s, y1: (l.y + l.h + 2.5) / s }))
    .map(l => ({ x: l.x0 - 1, y: l.y - 1.5, w: l.x1 - l.x0 + 2, h: l.h + 3 }))
}

function computeVeils() {
  const out = {}
  const outPage = {}
  const s = scale.value
  for (const it of flatItems.value) {
    if (it.origPage < 0) continue
    const el = pageEls.value[it.gi]
    if (!el) continue
    // 段落流之后的页（参考文献/附录）：整页灰。
    // 双重门：略读开着 + 段落流**已经加载**（刚打开论文时 store.paras 还是空的，
    // lastParaPage 是 -1，不加门会把每一页都当成"段落流之后的页"整页灰掉）。
    if (store.viewer.layers.skim && lastParaPage.value >= 0 &&
        it.origPage > lastParaPage.value && it.origPage > 0) {
      const boxes = pageRows(el, s)
        .filter(l => !onFigure(it.origPage, { x0: (l.x0 - 1) / s, x1: (l.x1 + 1) / s, y0: (l.y - 2.5) / s, y1: (l.y + l.h + 2.5) / s }))
        .map(l => ({ x: l.x0 - 1, y: l.y - 1.5, w: l.x1 - l.x0 + 2, h: l.h + 3 }))
      if (boxes.length) outPage[it.origPage] = boxes
      continue
    }
    const per = {}
    for (const p of parasByPage.value[it.origPage] || []) {
      if (!veiled(p, it.origPage)) continue
      const boxes = paraRowBoxes(p, el, s)
      if (boxes.length) per[p.idx] = boxes
    }
    out[it.origPage] = per
  }
  veilRects.value = out
  veilPageRects.value = outPage
}
function veilBoxes(p, pno) { return veilRects.value[pno]?.[p.idx] || [] }
/* 段落流之后的页（参考文献/附录）整页灰：pointer-events:none——不拦选字，
   想复制一条引文照样行；要细读就按 f 关掉略读。 */
function pageVeils(pno) { return veilPageRects.value[pno] || [] }

/* 蒙纱盖住的段不再画批注笔迹：那条"可跳过"的点线画在纱的**上面**（DOM 顺序靠后），
   透过纱看就是一排小黑点——用户原话"蒙的位置还有小黑点"。页边卡片照旧，
   掀开蒙纱照样能对上这段被谁标过。 */
function marksShownOnPage(pno) {
  const list = notesOnPage(pno)
  if (!store.viewer.layers.skim) return list
  return list.filter(n => !skimSkip.value.has(n.para_idx))
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
    // 译文页也有文字层：选中/复制译文是高频动作（origPage 仍为 -1——蒙纱、
    // 页边批注、框选都只认原文页，这个标记不改）
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
        // 交替模式下双语文档的第 j 页：偶数页是**原文第 j/2 页**（不是第 j 页）。
        // 写成 j 的话所有按"原文页号"索引的东西都会错一倍——段落蒙纱、批注卡、
        // 页码读数、查找命中、跳转全落错页（0 基 2i 当成了 i）。
        push([{ key: `di${j}`, doc: 'dual', page: j, origPage: isOrig ? j / 2 : -1, w: tm.w, h: tm.h, margin: isOrig, text: true }])
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
  try {
    await measure()
    await renderAll()
  } catch (e) {
    // 渲染中途炸了（某页画不出来等）：别让进度条永远爬、也别白屏不解释
    console.error('[eggpaper] 渲染失败：', e)
    toast('这份文档渲染失败了：' + String(e.message || e).slice(0, 120), 6000)
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
  else if (store.viewer.restorePos) { scroller().scrollTop = store.viewer.restorePos; store.viewer.restorePos = 0 }
  updateProg()
  // 让 scroll-spy 先跑一拍：不进滚动也要把页码、"读至 ¶n"、进度线初始化好
  if (veryFirst) setTimeout(onScroll, 800)
  if (pendingFind) {                      // 「文中」等在切回原文之后的那一次搜索
    const q = pendingFind
    pendingFind = null
    nextTick(() => { searchQ.value = q; searchOpen.value = true })
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
  if (seq === passToken) {
    rendering.value = false
    // 谁触发的那次渲染（缩放/换姿势/换篇）最后都要**重新量一遍标注**：渲染中途跑过的
    // measureNotes（比如 fit 一变引发的两条 renderAll 竞赛）是对着半套文字层量的，
    // 蒙纱和引文划线会缺块——以渲染收尾后的这一次为准。
    await measureNotes()
    // 搜索只扫已渲染的文本层：大 PDF 刚打开就搜，后半本的页还没渲染出来，
    // 会误报"没找到"。渲染齐了把活动查询再跑一遍，结果自己收敛。
    if (searchOpen.value && searchQ.value.trim().length >= 2) runSearch()
  }
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
        // 渲染是**流式**的：期间进来的查询（页边引文、略读蒙纱）会拿当时那半套文字层
        // 建索引并缓存——缓存键是这个元素，渲染完了元素没换，半套索引就一直被复用。
        // 这里再清一次：之后来的查询都会对着完整的文字层重建。
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

// 旁批高度靠实测：先按估算摆一遍，渲染后量真实高度再摆第二遍。
// 这样长批注展开后只会把下面的推开，不会压在别人身上。
function noteHeight(n) {
  const noteLines = Math.max(1, Math.ceil((n.note || '').length / 11))
  return 34 + Math.min(noteLines, 3) * 19 + 20
}

async function measureNotes() {
  await nextTick()
  computeQuoteMarks()
  computeVeils()
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
  // 用选中文字所在页兜底，para_idx 记 -1（不参与同段重钉去重）。
  // 译文/双语页的 origPage 是 -1：页号回退到它对应的原文页（mono 1:1、dual 奇偶折半），
  // 否则钉卡会带着 page=-1 落库，永远翻不到。
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
  // 没找到不弹 toast：输入是防抖逐字触发的，中间态（"自组"→"自组装"）会弹假警报；
  // 结果条本身的「无结果」就是答案
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
let pendingFind = null       // 等这一轮渲染完再搜（换模式要重新出图、建文字层）
function findInPaper(q) {
  if (!q) return
  // 查找只在原文的文字层里找，而译文页与框选模式下**没有** .textLayer：
  // 直接搜必然"没找到"，用户会以为这个词不在这篇里
  if (store.viewer.variant !== 'original') {
    store.viewer.variant = 'original'
    toast('已切回原文再找')
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
  loadFigs()
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
// 档位筛选会改"页边要不要留"（四档全关 = 整条页边消失），而页宽是按这个定标的：
// 不重排的话论文不会变宽，只是整块往右挪，白白空掉两百像素
watch(() => store.viewer.noteBands, () => reflow(), { deep: true })
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
                 :data-page="it.origPage" :data-scale="scale"
                 :style="{ width: it.w * scale + 'px', height: it.h * scale + 'px' }"
                 @mousedown="e => startFrameDrag(e, it)">

            <canvas :ref="el => (canvases[it.gi] = el)"></canvas>
            <div class="care-wash" v-if="store.viewer.care !== 'off'"></div>
            <!-- 文字层**不随框选卸载**（只关掉它的鼠标/可见性）：v-if 一摘一挂，
                 新元素是空的，而 doneKeys 还记着"这一档渲染过了"→ 再也不会重画，
                 退掉框选之后划词、查找、引文划线全哑（元素在、里面一个字都没有）。 -->
            <div class="textLayer" v-if="it.text" :class="{ 'tl-off': store.viewer.frame }"
                 :ref="el => (textLayers[it.gi] = el)"></div>
            <div v-if="frameRect && frameRect.gi === it.gi" class="frame-rect"
                 :style="{ left: Math.min(frameRect.x0, frameRect.x1) + 'px', top: Math.min(frameRect.y0, frameRect.y1) + 'px',
                           width: Math.abs(frameRect.x1 - frameRect.x0) + 'px', height: Math.abs(frameRect.y1 - frameRect.y0) + 'px' }"></div>

            <div class="para-zone">
              <!-- 参考文献区/附录（段落流之后的页）整页灰：不拦鼠标，字可以照常选中 -->
              <div v-for="(vb, vi) in pageVeils(it.origPage)" :key="'pv' + vi"
                   class="para-fade veil page-veil"
                   :style="{ left: vb.x + 'px', top: vb.y + 'px', width: vb.w + 'px', height: vb.h + 'px',
                             animationDelay: Math.min(400, vi * 8) + 'ms' }"></div>
              <template v-for="(p, pi) in parasByPage[it.origPage] || []" :key="'f' + p.idx">
                <!-- 灰掉的段落：悬停掀开看一眼（CSS），点一下=「这段我也要读」 -->
                <template v-if="veiled(p, it.origPage)">
                  <div v-for="(vb, vi) in veilBoxes(p, it.origPage)" :key="'v' + vi"
                       class="para-fade veil" :title="vi ? '' : '这段也要读'"
                       :style="{ left: vb.x + 'px', top: vb.y + 'px', width: vb.w + 'px', height: vb.h + 'px',
                                 animationDelay: Math.min(400, pi * 12 + vi * 8) + 'ms' }"
                       @mousedown="veilDown = { x: $event.clientX, y: $event.clientY }"
                       @click="onVeilClick($event, p.idx)"></div>
                </template>
                <!-- 留下来的段落只在页边留一道芯线（不写字）。手选过的段落，这条芯线
                     就是撤销出口：纸上没有别的可点的地方了。 -->
                <div v-else-if="store.viewer.layers.skim && it.origPage > 0 && (isCore(p) || kept(p.idx))"
                     class="para-core-bar" :class="{ undo: kept(p.idx) }"
                     :title="kept(p.idx) ? '取消保留' : ''"
                     :style="{ top: p.bbox.y0 * scale + 'px', height: (p.bbox.y1 - p.bbox.y0) * scale + 'px' }"
                     @click.stop="kept(p.idx) && toggleKeep(p.idx)"></div>
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
              <template v-for="{ n } in marksShownOnPage(it.origPage)" :key="'n' + n.id">
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
                <button v-if="!pending" class="mg-ask" @click.stop="askNote(n)">问 ↗</button>
                <button v-if="!pending" class="mg-del" @click.stop="unpin(n.id)">×</button>
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
                <span v-if="quoteLoose(n)" class="mg-loose" title="引文与原文略有出入">≈</span>
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
               @keydown.enter="gotoPage(Number(pageIn)); pageInputEl?.blur()" @blur="pageIn = String(pageNum)" />
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
