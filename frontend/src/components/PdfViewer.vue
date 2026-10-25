<script>
/* 解析好的 PDF 文档缓存（docCache）与"译文被重写后作废它"的 dropPaperDocs 都在 store 里：
   重译完成那一刻要在**任何**位置作废它（含"人在别的篇、后台译完"的那种），不只是本组件。
   这里只留实例自己的登记表。 */
const docClaims = new Map()   // 实例令牌 → 它正用着的 key 集（见 getDoc 的逐出循环）
</script>

<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, askNotePrefill, kindZH, paperName, docCache, dropPaperDocs, focusSoon } from '../store'
import { confirmBox } from '../dialog'
import { lsGet, lsSet } from '../ls'
import { copyWithToast, frameBuffer } from '../clip'
import { scrollBehavior } from '../motion'

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
import { lineSpanOf, findQuoteRects, findAllRects, findDocLinks, clearTextIndex, sentenceAround } from '../find'
import { prettyChem } from '../chem'
import { translateStream } from '../api'
import MdLite from './MdLite.vue'
import EggMark from './EggMark.vue'
import { vDrag } from '../drag'
import { t, isEn, ui } from '../i18n'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

const GUTTER_FULL = 184   // 页边批注带（184 宽 + 12 的左边距）

const deskEl = ref(null)
const ready = ref(false)
const loadPct = ref(0)         // 破壳进度：喂给入场那条细线
let creep = 0                   // 缓慢逼近的底速：本地加载瞬时就绪、真进度来得太快,
                                // 每 240ms 向 92% 挪、封顶 90%（startCreep/stopCreep）
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
/* 每页的 DOI / arXiv 原文链接（文本层建好时现算，缩放重建自动跟着重算） */
const docLinks = ref([])
const doneKeys = new Set()
const renderQueues = new Map()   // gi -> 渲染链尾：同一画布严格串行，杜绝并发 render
let passToken = 0
const scroller = () => deskEl.value?.closest('.desk') || deskEl.value
let ro = null

/* 装载/补画的超时与间隔：值即契约，改之前先看 visual §12 与各引用处注释 */
const PDF_WORKER_TIMEOUT_MS = 20000  // pdf.js worker 无响应判死
const JUMP_STALE_MS = 8000           // 渲染没赶上的跳转最多等 8 秒
const FILL_GAP_MS = 180              // 闲时补画两页之间的间隔（visual §12 定死）

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
/* 本窗格的段落表（idx → 段）。多窗格时全局 paraByIdx 跟踪的是**活动篇**，
   非活动窗格拿它查锚点等于用别篇的段落坐标：引文高亮画到不相干的 y、
   页边卡回落的位置全乱、跳转跳错行。锚点一律以本窗格自持的 paras 为准。 */
const byIdx = computed(() => {
  const m = {}
  for (const p of paras.value) m[p.idx] = p
  return m
})
/* 页边摆哪些批注：读者自己钉的（查译/框选答疑/自己写的批注）是**读者资产**，
   唯一的开关是设置里的「页边批注」——默认显示，想收起去设置里点一下。 */
const USER_KINDS = new Set(['lookup', 'region', 'note'])
const notesShown = computed(() => {
  const mineOn = store.viewer.layers.mine !== false
  return mnotes.value.filter(n => USER_KINDS.has(n.kind) && mineOn)
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
  // worker 会假死（休眠恢复/标签页冻结/内嵌环境节流）：promise 永不落定，纸面永久白屏。
  // 看门狗 20s：超时就销毁这个 task 换新 worker 重来一次；再挂才向上抛（load 的 catch 接住）。
  for (let attempt = 0; attempt < 2; attempt++) {
    // CJK 非嵌入字体（UniGB-UTF16-H 这类中文 PDF 常见）要靠外部 CMap 解码，
    // 标准 14 字体要靠 .pfb 度量——两样都在 public/ 里随包发布（见下方注），
    // 不配的话中文件整本白纸 + 文字层全空，还静默无报错（2026-10-16 走查抓到）。
    const task = pdfjsLib.getDocument({
      url: `/api/papers/${props.pid}/pdf?variant=${kind}`,
      cMapUrl: "/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "/standard_fonts/",
    })
    if (!sheets.value.length) {
      task.onProgress = ({ loaded, total }) => {
        if (total) loadPct.value = Math.max(loadPct.value, Math.min(0.94, loaded / total))
      }
    }
    const doc = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('pdf worker timeout')), PDF_WORKER_TIMEOUT_MS)
      task.promise.then(
        d => { clearTimeout(timer); resolve(d) },
        e => { clearTimeout(timer); reject(e) })
    })
      .catch(e => {
        try { task.destroy() } catch { /* 已死不碍事 */ }
        if (attempt === 0 && String(e.message).includes('timeout')) return null   // 换 worker 重试
        throw e
      })
    if (!doc) continue
    docCache.set(key, { doc, at: Date.now() })
    if (docCache.size > 10) {
      const used = new Set()
      for (const s of docClaims.values()) for (const k of s) used.add(k)
      while (docCache.size > 10) {
        let oldest = null, ot = Infinity
        for (const [k, e] of docCache) if (e.at < ot && !used.has(k)) { ot = e.at; oldest = k }
        if (oldest == null) break               // 全是活窗格在用的：别销，销了那格就白屏
        const e = docCache.get(oldest)
        try { e.doc.destroy() } catch { /* */ }
        docCache.delete(oldest)
      }
    }
    return doc
  }
  throw new Error('pdf worker 连续两轮 20s 无响应')   // 不写这行：函数静默返回 undefined，
}                                                      // 上层报「Cannot read properties of undefined」天书

async function meta(doc) {
  const vp = await (await doc.getPage(1)).getViewport({ scale: 1 })
  return { count: doc.numPages, w: vp.width, h: vp.height }
}

const claimTok = Symbol()   // 本实例在 docClaims 里的登记号

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
    /* 双语直接用 原文+译文 两份文档排，不再取后端那份"整本交错"的 dual.pdf：
       老链路首开要在请求线程里现场合成它（真库 4~24 页小件实测 0.02~0.12s，
       240 页大部头构造+保存合计 1~5s，见 lesson §三），且要整本下载——体积 ≈
       原文+译文之和，对开姿势只用到它的译文偶数页，一半是白下的（实测 20 页件
       对开 22.9MB → 8.0MB）。orig 多半已在 docCache（原文是默认变体），首开只补
       一份 mono：零合成 CPU、字节近乎减半。
       译文页数与原文不齐的异常件按 derive_dual 同款兜底：译文缺的页用原文页
       占位（奇数行 doc=orig），"偶=原文、奇=译文"的页码关系不变。 */
    const [od, md] = await Promise.all([getDoc('orig'), getDoc('mono')])
    const [om, mm] = await Promise.all([meta(od), meta(md)])
    const n = Math.max(om.count, mm.count)
    if (store.viewer.spread === 'spread') {
      for (let i = 0; i < n; i++) {
        const hasTr = i < mm.count
        const op = Math.min(i, om.count - 1)
        push([
          { key: `sl${i}`, doc: 'orig', page: op, origPage: op, w: om.w, h: om.h, margin: false, text: true },
          { key: `sr${i}`, doc: hasTr ? 'mono' : 'orig', page: hasTr ? i : op, origPage: -1,
            w: hasTr ? mm.w : om.w, h: hasTr ? mm.h : om.h, margin: false, text: true },
        ])
      }
    } else {
      for (let j = 0; j < 2 * n; j++) {
        const i = j >> 1, isOrig = !(j & 1)
        const op = Math.min(i, om.count - 1)
        const tr = !isOrig && i < mm.count
        push([{ key: `di${j}`, doc: tr ? 'mono' : 'orig', page: tr ? i : op,
          origPage: isOrig ? op : -1, w: tr ? mm.w : om.w, h: tr ? mm.h : om.h,
          margin: isOrig, text: true }])
      }
    }
  }
  sheets.value = rows
  const keys = new Set()
  for (const s of sheets.value) for (const it of s.items) keys.add(`${props.pid}:${it.doc}`)
  docClaims.set(claimTok, keys)   // 这批 doc 本窗格在用：LRU 逐出时绕开（卸载时撤销登记）
}

function startCreep() {
  stopCreep()
  creep = setInterval(() => {
    if (loadPct.value >= 0.9) return
    loadPct.value = Math.min(0.9, loadPct.value + (0.92 - loadPct.value) * 0.22)
  }, 240)
}
function stopCreep() { clearInterval(creep); creep = 0 }

let loadSeq = 0    // 装载代次：变体/排布连切时，先发的 load 慢一步回来的话会把旧布局
                   // 覆盖回去（sheets 赋值没有守卫），屏幕停在用户早已离开的模式上

async function load({ keepPlace = false } = {}) {
  const seq = ++loadSeq
  /* 这篇没有成品（translate_status 不是 done）时，别带着"上一本留下的"译文/双语变体
     去取 PDF：那会白打一发 /pdf?variant=mono|dual 的 404，然后弹一句"译文版打不开"
     ——用户看到的是一场并不存在的故障（实测：双语态下打开没翻译的中文论文）。
     变体是跨论文记的全局偏好；多窗格/同屏切换是刻意**不**动它的（别扰邻格），
     所以这道闸要留在拿到 PDF 之前。查这一篇自己的行，不查"当前篇"。
     查不到行就不拦（列表还没到）：那种情况有既有的 catch 兜底。 */
  const row = (store.papers || []).find(p => p.id === props.pid)
  if (store.viewer.variant !== 'original' && row && row.translate_status !== 'done') {
    store.viewer.variant = 'original'      // watch 会重新 load；本次就地收手
    loading = false
    return
  }
  loading = true
  const veryFirst = !sheets.value.length
  if (veryFirst) { ready.value = false; loadPct.value = 0.08; startCreep() }
  anchorCancel = false          // 新一轮装载从零记"用户动没动过"：别吃上一轮的旧旗
  const anchor = keepPlace || !veryFirst ? currentAnchor() : null
  sheets.value = []
  doneKeys.clear()
  doneTxt.clear()
  paintGen++        // 旧布局的闲时补画链到此为止（它画的是已被换掉的页）
  try {
    await buildSheets()
  } catch (e) {
    if (seq !== loadSeq) return      // 已被更新的装载顶掉：这份是过站的，别再碰任何状态
    if (store.viewer.variant !== 'original') {
      const was = store.viewer.variant
      store.viewer.variant = 'original'      // 赋值会触发 watch → 重新 load
      if (was === 'mono') toast(t('译文版打不开，已切回原文'), 5000)
      else toast(t('双语版打不开，已切回原文'), 5000)
      loading = false
      return
    }
    toast(t('文档加载失败：{m}', { m: e.message }))
    ready.value = true
    loading = false
    return
  }
  if (seq !== loadSeq) return
  let settled = false
  const settle = () => {
    if (settled || seq !== loadSeq) return
    settled = true
    stopCreep()
    loadPct.value = 1
    ready.value = true
    // 同屏切换（单篇→多窗格）会卸载本实例：进行中的 load 尾巴还在跑，
    // scroller() 已经是 null——落位作罢，别把卸载当渲染失败惊扰用户
    if (!scroller()) return
    if (anchorCancel) { /* 装载期间用户已自行跳位：旧锚点/旧滚动位都不许顶掉它 */ }
    else if (anchor) applyAnchor(anchor)
    else if (savedScroll != null) { scroller().scrollTop = savedScroll }
    updateProg()
  }
  try {
    await measure()
    /* 画哪一带：换模式按锚点（落位要跳回的那一页），首开按记下来的滚动位置。
       不带这个的话，画的是"旧滚动位置"那一带，落位一跳又得重画一遍。 */
    let focusIt = anchor ? pageItem(anchor.page) : null
    if (!focusIt && savedScroll != null && scroller()) {
      const a = anchorAt(savedScroll + 8)
      if (a) focusIt = pageItem(a.page)
    }
    await renderAll({ early: settle, focusGi: focusIt ? focusIt.gi : null })
  } catch (e) {
    if (seq !== loadSeq) return
    console.error('[eggpaper] 渲染失败：', e)
    toast(t('渲染失败：{m}', { m: String(e.message || e).slice(0, 120) }), 6000)
    stopCreep()
    loadPct.value = 1
    ready.value = true
    loading = false
    return
  }
  if (seq !== loadSeq) { loading = false; return }
  settle()                 // 只有一两页的薄文档，early 的打点可能赶不上，这里兜住
  /* 兜底：这一轮的带是按预判挑的，落位后再验一次眼前这几页真在不在——
     不在就让滚动补画那条路补上。少了它，预判偏了（记忆位置与当前排版对不上）
     就是一屏空白纸，用户不滚动还触发不了补画。 */
  ensureNear()
  await nextTick()
  await measureNotes()
  if (veryFirst) setTimeout(onScroll, 800)
  if (pendingFind) {                      // 「文中」等在切回原文之后的那一次搜索
    const q = pendingFind
    pendingFind = null
    nextTick(() => { searchQ.value = q; searchOpen.value = true })
  }
  if (pendingJump) {                      // 渲染没赶上的那次跳转（跨篇《标题》¶n 常见）
    if (store.jump === pendingJump && Date.now() - pendingJump.at < JUMP_STALE_MS) applyJump()
    pendingJump = null
  }
  loading = false
}

/* 页码定位。三种模式的页号不是一回事：
   原文页 i / 译文第 i 页（1:1）/ 双语行 2i=原文、2i+1=译文（译文行 origPage=-1，
   mono 的页序与原文一一对应，it.page 即原文页；异常件的原文占位行同理）。
   统一换算成"原文第几页"，跳转和位置记忆才不会跨模式错位。 */
function origPageOf(it) {
  if (!it) return 0
  if (it.origPage >= 0) return it.origPage
  return it.page
}
/* "原文页码 → 项"的索引：两趟优先级（第一趟认 origPage、第二趟退到 mono；
   每趟 first-wins，先命中的不被后面覆盖）。pageItem 会被引文落位、批注锚点、
   段落跳转按条调用，每条最多两趟全量 find。 */
const pageItemIndex = computed(() => {
  const m = new Map()
  const items = flatItems.value
  for (const it of items) if (it.origPage >= 0 && !m.has(it.origPage)) m.set(it.origPage, it)
  for (const it of items) if (it.doc === 'mono' && !m.has(it.page)) m.set(it.page, it)
  return m
})
function pageItem(pno) {
  return pageItemIndex.value.get(pno) || null
}

/* 当前读到哪：原文页码 + 页内高度比例（比例让不同缩放/排布之间也能对上） */
function currentAnchor() {
  const sc = scroller()
  return sc ? anchorAt(sc.scrollTop + 8) : null
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
  /* 可用宽从 DOM 实读（.desk-inner 的左右内边距 + 对开时的列间距），再留 20px
     滚动条/取整余量：这些数字以前是 60/20 两个魔法数，样式一改「适宽」就悄悄算偏，
     页边卡被挤出视口 */
  const inner = sc.querySelector('.desk-inner')
  let padX = 40
  if (inner) {
    const cs = getComputedStyle(inner)
    padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0)
  }
  let gap = 18
  if (perRow === 2 && inner) {
    const row = inner.querySelector('.spread-row')
    if (row) {
      const rs = getComputedStyle(row)
      gap = parseFloat(rs.columnGap) || parseFloat(rs.gap) || 18
    }
  }
  fitScale.value = (sc.clientWidth - padX - 20 - gutter - (perRow === 2 ? gap : 0)) / (first.w * perRow)
  pageFitScale.value = (sc.clientHeight - 56) / first.h
}

function updateMid() {
  const el = scroller()
  if (!el) return
  const r = el.getBoundingClientRect()
  midX.value = Math.round(r.left + r.width / 2)
  deskW.value = Math.round(r.width)
  deskRightX.value = Math.round(r.right - 3)
}

let reflowT = null, reflowAnchor = null, loading = false
function reflow() {
  if (loading) return                       // 换模式那次由 load() 负责落位，别抢
  if (reflowT == null) {
    reflowAnchor = currentAnchor()
    anchorCancel = false    // 开一扇新的"捕获后用户动没动"窗：别吃更早交互留下的旧旗
  }
  clearTimeout(reflowT)
  reflowT = setTimeout(async () => {
    reflowT = null
    measure()
    await renderAll()
    await measureNotes()
    /* 这 200ms 里用户自己跳了页/滚了轮：捕获时刻的旧锚点作废，但位置仍要按新
       尺寸翻译——用"此刻"的锚点回位，人在哪一页就还落在哪一页。 */
    const a = reflowAnchor && !anchorCancel ? reflowAnchor : currentAnchor()
    if (a) applyAnchor(a)
    reflowAnchor = null
  }, 200)
}

/* 首轮出图＝**只画视口那一带**（band 页），其余交给两条闲时/按需的路补：
     · 滚动到哪画到哪（ensureNear → near pass）；
     · 闲着一页一页往外套（fillCanvasesIdle），套到预算装得下的页数为止。
   以前换模式/缩放是整本重光栅化：本机实测 120 页扫描件一次 4.4~7.4s、峰值位图 452MB；
   真库里 24 页的论文切一次也要 4s（24 页 ×~150ms）。弱机 dpr 1.5 时像素再 ×2.25，
   就是用户报的"点一下卡几十秒、鼠标都动不了"。现在一次切换只画几页。
   文字层另走一路（fillTextLayers/fillTextLayersIdle）——搜索、批注引文、DOI 链接只认它。 */
async function renderAll({ early = null, near = false, focusGi = null } = {}) {
  const seq = ++passToken
  rendering.value = !near        // 近距补画是滚动跟手的一部分，别让顶部细线闪个不停
  await nextTick()
  // 渲染顺序 = 离视口中心由近到远：眼前几页先回来（顶上那条细进度条跑到头为止）。
  // 换模式时中心认锚点（要跳回的那一页），不然会画在旧滚动位置那一带、落位后还得重画。
  const items = flatItems.value.slice()
  const centerGi = focusGi == null ? _centerGi() : focusGi
  items.sort((a, b) => Math.abs(a.gi - centerGi) - Math.abs(b.gi - centerGi))
  const { band, keep } = canvasPlan()
  const inBand = it => Math.abs(it.gi - centerGi) <= band
  let todo
  if (near) todo = items.filter(it => inBand(it) && !doneKeys.has(doneKeyOf(it)))
  else todo = items.slice(0, Math.min(band, items.length))
  const skipKeys = near ? null : new Set(todo.map(doneKeyOf))
  let kicked = false
  for (let i = 0; i < todo.length; i++) {
    if (seq !== passToken) return
    await renderItem(todo[i], seq)
    if (skipKeys) skipKeys.delete(doneKeyOf(todo[i]))   // 画过的页可以被逐出：本轮不会再回头看它
    if (!kicked && early && (i >= 2 || i === todo.length - 1)) {
      kicked = true
      early()                      // 视口那几页落地就掀壳（首开）/落位（换排版），不等整本
    }
    if (!near && (i & 7) === 7) evictFar(skipKeys)   // 大部头边画边收，位图不攒到 GB 级
  }
  if (seq === passToken) {
    rendering.value = false
    if (!near) {
      evictFar(skipKeys)
      fillCanvasesIdle()            // 带外闲时一页一页补（节流，不跟滚动/输入抢）
      fillTextLayersIdle()          // 带外只闲时补文字层：纸面的字可以慢慢画，搜索/引文不能缺
      if (seq !== passToken) return
      await measureNotes()
      if (searchOpen.value && searchQ.value.trim().length >= 2) runSearch()
    }
  }
}

/* ---------------- 闲时补画（带外页面） ----------------
   一页一页来、两页之间还留间隔（180ms）：把"整轮光栅化"摊成背景里的小步，
   机器不会有一段时间被顶住。有一轮 pass 在跑时让它先画（用户的滚动最优先）。 */
let paintGen = 0, paintAt = 0
const IDLE = window.requestIdleCallback || (cb => setTimeout(() => cb({ timeRemaining: () => 8 }), 120))

function _nextUnpainted() {
  const items = flatItems.value
  const { band, keep } = canvasPlan()
  let painted = 0
  for (const it of items) if (doneKeys.has(doneKeyOf(it))) painted++
  if (painted >= keep) return null                 // 预算装满就停：更远的页交给滚动补画
  const c = _centerGi()
  const near3 = band * 3                           // 只补视口附近这几屏，跳转后的远页等滚过去再画
  let best = null, bestD = 1e9
  for (const it of items) {
    if (doneKeys.has(doneKeyOf(it))) continue
    const d = Math.abs(it.gi - c)
    if (d <= near3 && d < bestD) { bestD = d; best = it }
  }
  return best
}

function fillCanvasesIdle() {
  const my = ++paintGen
  const step = () => {
    if (my !== paintGen) return
    if (rendering.value) { IDLE(step, { timeout: 600 }); return }   // 有 pass 在跑：让它先画
    const it = _nextUnpainted()
    if (!it) return
    const go = () => {
      if (my !== paintGen || rendering.value) { if (my === paintGen) IDLE(step, { timeout: 900 }); return }
      paintAt = performance.now()
      renderItem(it, passToken).catch(() => {})
      IDLE(step, { timeout: 900 })
    }
    const wait = Math.max(0, FILL_GAP_MS - (performance.now() - paintAt))
    if (wait > 0) setTimeout(go, wait)
    else go()
  }
  IDLE(step, { timeout: 600 })
}

function doRenderItem(it, seq) {
  const key = it.key + ':' + scale.value.toFixed(3)
  if (doneKeys.has(key)) return
  if (!flatItems.value.includes(it)) return          // 这一项已被新布局替换
  const canvas = canvases.value[it.gi], tlEl = textLayers.value[it.gi], el = pageEls.value[it.gi]
  if (!canvas || !el) return
  const doc = getDoc(it.doc)
  // 渲染看门狗 45s：单页假死时让出串行队列（不标记 doneKeys，下一轮渲染会重试该页），
  // 别让一页卡死把整条渲染链和 ready 落位永远堵死。定时器在 race 出结果后要清掉——
  // 不然 600 页的书每轮渲染留下 600 个 45s 的空转定时器。
  let wd = 0
  const work = doc.then(async d => {
      if (seq !== passToken) return        // pass 已过期：这一页的缩放/布局已经换了，别再画
      const page = await d.getPage(it.page + 1)
      const viewport = page.getViewport({ scale: scale.value })
      const dpr = dprFor(viewport.width, viewport.height)
      canvas.width = Math.floor(viewport.width * dpr)
      canvas.height = Math.floor(viewport.height * dpr)
      canvas.style.width = viewport.width + 'px'
      canvas.style.height = viewport.height + 'px'
      el.style.setProperty('--scale-factor', scale.value)
      const ctx = canvas.getContext('2d', { alpha: false })
      ctx.fillStyle = '#fff'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      const task = page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null })
      if (seq !== passToken) {             // 起跑前再验一次 pass：连续缩放时旧任务的 CPU 不白烧
        task.cancel()
        task.promise.catch(() => {})       // 取消后的拒绝已经没人等了，必须有人接住
        return
      }
      try {
        await task.promise
        if (tlEl && it.text) await buildText(it, tlEl, el, page, viewport)
        else if (docLinks.value[it.gi]?.length) {
          docLinks.value[it.gi] = []                   // 无文本层的变体（扫描件态）：别留上一版的链接
        }
        doneKeys.add(key)
      } catch (err) {
        if (String(err?.name || err?.message || '').toLowerCase().includes('cancel')) return
        console.error('[eggpaper] render fail', it.key, err)
      }
  }).catch(err => console.error('[eggpaper] render fail', it.key, err))
  return Promise.race([
    work,
    new Promise(resolve => { wd = setTimeout(resolve, 45000) }),
  ]).finally(() => clearTimeout(wd))
}

/* 文字层：搜索、划词、批注引文的地基。跟着位图一起建（都画），或者单独建（带外页面）。 */
async function buildText(it, tlEl, el, page, viewport) {
  tlEl.innerHTML = ''
  clearTextIndex(tlEl)                 // 节点全换了，旧的引文索引作废
  el.style.setProperty('--scale-factor', scale.value)
  const tl = new pdfjsLib.TextLayer({ textContentSource: page.streamTextContent(), container: tlEl, viewport })
  await tl.render()
  clearTextIndex(tlEl)
  docLinks.value[it.gi] = findDocLinks(el)     // 链接层跟着文本层走，一次微秒级的正则扫描
  doneTxt.add(doneKeyOf(it))
}

/* 带外页面的文字层补建：只建文字层，不碰画布（大部头里画布才是贵的那部分）。
   两条路，同一个代次号（新的打断旧的）：
     · 查找前全量补——缺层会让 Ctrl+F **静默漏页**，这一路必须跑完；
     · 平时闲时一片一页地补——不跟滚动/输入抢主线程，几秒后整本也就齐了。 */
let fillGen = 0

function _nextMissingText() {
  for (const it of flatItems.value) {
    if (it.text && !doneTxt.has(doneKeyOf(it)) && textLayers.value[it.gi] && pageEls.value[it.gi]) return it
  }
  return null
}

async function _buildTextOf(it, gen) {
  const tlEl = textLayers.value[it.gi], el = pageEls.value[it.gi]
  if (!tlEl || !el) return
  const d = await getDoc(it.doc)
  const page = await d.getPage(it.page + 1)
  if (gen !== fillGen) return
  await buildText(it, tlEl, el, page, page.getViewport({ scale: scale.value }))
}

async function fillTextLayers() {
  const my = ++fillGen
  const items = flatItems.value
  for (let i = 0; i < items.length; i++) {
    if (my !== fillGen) return
    const it = items[i]
    if (!it.text || doneTxt.has(doneKeyOf(it))) continue
    try {
      await _buildTextOf(it, my)
    } catch (e) {
      console.error('[eggpaper] text layer fail', it.key, e)
    }
    if ((i & 7) === 7) await new Promise(r => setTimeout(r, 0))   // 分部头：每 8 页让出主线程
  }
}

function fillTextLayersIdle() {
  const my = ++fillGen
  const idle = IDLE
  const step = async () => {
    if (my !== fillGen) return
    const it = _nextMissingText()
    if (!it) return
    try {
      await _buildTextOf(it, my)
    } catch (e) {
      console.error('[eggpaper] text layer fail', it.key, e)
    }
    if (my === fillGen) idle(step, { timeout: 600 })
  }
  idle(step, { timeout: 600 })
}

function renderItem(it, seq) {
  const prev = renderQueues.get(it.gi) || Promise.resolve()
  const task = prev.then(() => doRenderItem(it, seq))
  renderQueues.set(it.gi, task.catch(() => {}))
  return task
}

function scheduleRender(near = false) {
  clearTimeout(scheduleRender._t)
  scheduleRender._t = setTimeout(() => renderAll(near ? { near: true } : {}), 160)
}

/* ---------------- 位图记账：常驻上限 + 视口补画 ----------------
   600 页的大部头全画完时，每页一张满分辨率位图常驻，内存以 GB 计（缩放 300% 时
   单页就能上百 MB）。文字层是搜索/划词的地基，永远保留；只收位图——被收掉的页
   滚回来时 ensureNear 补画。小文档（整本在预算内）完全不进这条路，行为与从前一致。 */
const KEEP_NEAR = 6
const KEEP_CANVASES = 36        // 常驻页数上限（预算算出来比它多时也照它砍）
// 位图常驻预算按机器内存分档：deviceMemory 是 Chromium 系暴露的近似 GB（Safari/
// Firefox 没有它，取不到按高档走——预算大顶多多占点，按低档走会让大屏多页常驻
// 被误砍）。≤4GB 的低配机上 pdf.js worker 里还压着整本解析树与字体，同留 192MB
// 位图就容易把系统挤进页交换，表现是"滚得越多越卡"；减半后只是带外少留几页、
// 滚回来现画，纸面永远只画视口那一带，行为不劣化。
const PIX_BUDGET = ((navigator.deviceMemory || 8) <= 4 ? 96 : 192) * 1024 * 1024
const MAX_CANVAS_PX = 8e6                // 单张画布像素上限：4K + 200% 缩放 + 放大到 300% 时
                                         // 一张就是上亿像素（几百 MB），比整个预算还大
const doneKeyOf = it => it.key + ':' + scale.value.toFixed(3)
const doneTxt = new Set()                // 文字层单独记账：位图可以收，文字层不收

function dprFor(w, h) {
  let d = Math.min(2.5, window.devicePixelRatio || 1)
  if (w * h * d * d > MAX_CANVAS_PX) d = Math.max(1, Math.sqrt(MAX_CANVAS_PX / (w * h)))
  return d
}

/* 一屏要花多少位图：{band 附近保几页（滚动跟手）, keep 全局留几页}。
   按"预算 ÷ 单页字节"算，不是拍脑袋的页数——同一份预算，小屏多留几页、
   4K 高 DPI 少留几页，内存曲线才是平的（弱机 + 高 DPI 曾经是 36 页 × 30MB）。 */
function canvasPlan() {
  const it = flatItems.value[0]
  const w = (it?.w || 800) * scale.value, h = (it?.h || 1100) * scale.value
  const dpr = dprFor(w, h)
  const per = Math.max(1, Math.floor(w * dpr) * Math.floor(h * dpr) * 4)
  const pages = Math.max(1, Math.floor(PIX_BUDGET / per))
  // dpr 只在这里参与算字节预算：外面要的是 band/keep（画布自己按 dprFor 再算一遍）
  return { band: Math.max(3, Math.min(KEEP_NEAR + 2, Math.floor(pages / 2))),
           keep: Math.max(5, Math.min(KEEP_CANVASES, pages)) }
}

function _centerGi() {
  /* 与 pageIdxAtTop 是同一道题（"最后一个 offsetTop ≤ Y 的项"），直接复用它的二分：
     线性回退的"跳过未挂载、遇到挂载且越界即断"与本函数原先的手写循环逐字同构。 */
  const sc = scroller()
  if (!sc || !flatItems.value.length) return 0
  return pageIdxAtTop(sc.scrollTop + sc.clientHeight / 2)
}

function evictFar(skipKeys = null) {
  const items = flatItems.value
  const { band, keep } = canvasPlan()
  if (items.length <= keep) return
  const c = _centerGi()
  const done = items.filter(it => doneKeys.has(doneKeyOf(it)))
  if (done.length <= keep) return
  done.sort((a, b) => Math.abs(b.gi - c) - Math.abs(a.gi - c))     // 离视口最远的先收
  for (let i = 0; i < done.length - keep; i++) {
    const it = done[i]
    const k = doneKeyOf(it)
    if (skipKeys && skipKeys.has(k)) continue     // 这一轮还没画到它：收了它马上又会重画
    if (Math.abs(it.gi - c) <= band) continue     // 带内是滚动跟手要用的：不动它
    releaseCanvas(canvases.value[it.gi])   // 见 releaseCanvas 的注释：内联尺寸必须一起撤
    doneKeys.delete(k)
  }
}

/* 缩放那一拍：纸框尺寸是即时绑定（it.w * scale），位图要等重渲染才跟上，中间
   至少一拍（防抖 160ms 起）画布还是旧尺寸——纸面右侧/下方留出一条白边，放大时
   尤其明显。把在册画布的内联尺寸先按新缩放拉到位：位图旧一拍、糊一点，
   但没有白边、没有错位（重渲染到了就被新位图替换）。 */
function stretchCanvases() {
  for (const it of flatItems.value) {
    const canvas = canvases.value[it.gi]
    if (!canvas || canvas.width <= 1) continue
    canvas.style.width = it.w * scale.value + 'px'
    canvas.style.height = it.h * scale.value + 'px'
  }
}

/* 缩放/换排版后，带外的位图还停在旧比例上：尺寸换来换去反正都要重画，
   先撤掉——留着的是错位的图（放大后只占左上角、缩小后溢出纸面）。 */
/* 撤掉一张画布的位图：位图尺寸置 1 是"释放这几 MB"，内联尺寸必须同时清掉——
   留着 1×1 的内联尺寸会被 CSS 拉满整页，就是一张不透明的黑纸（实测踩过）。 */
function releaseCanvas(canvas) {
  if (!canvas || canvas.width <= 1) return
  canvas.width = 1
  canvas.height = 1
  canvas.style.width = ''
  canvas.style.height = ''
}

function dropFarCanvases() {
  const items = flatItems.value
  const { band } = canvasPlan()
  if (items.length <= band) return
  const c = _centerGi()
  for (const it of items) {
    if (Math.abs(it.gi - c) <= band) continue
    releaseCanvas(canvases.value[it.gi])
  }
}

function ensureNear() {
  if (rendering.value) return    // 渲染 pass 在跑：它由近到远自己会画到这一页，别打断它
  if (!sheets.value.length || !scroller()) return
  const { band } = canvasPlan()
  const c = _centerGi()
  for (const it of flatItems.value) {
    if (Math.abs(it.gi - c) > band) continue
    if (!doneKeys.has(doneKeyOf(it))) { scheduleRender(true); return }
  }
}

/* ---------------- 标注几何 ---------------- */

/* ---------------- 段落矩形落位：原文页直取，译文页按页内比例 ---------------- */

/* 这一项（窗格里的一张页）画的是原文第几页的段区：原文页与译文版（mono）都是页号
   本身（babeldoc 的 mono 输出页序与原文一一对应），双语里的译文行也是 mono 页号
   即原文页。译文页也要画段区——不然译文版面里点 ¶n 跳过来什么也看不见。 */
function paraPageOf(it) {
  if (it.origPage >= 0) return it.origPage
  if (it.doc === 'mono') return it.page
  return -1
}

/* 段落矩形落在这一项上的坐标。译文页按**页内比例**换算（原文 y/页高 → 译文页
   同比例，x 同理）：译文版重排过版面，页高与原文不一定相等，直接搬 y 会整体漂；
   原文页上 bbox 自带的 pw/ph 与页元素同尺寸，比例天然是 1。 */
function paraBoxOn(p, it) {
  const b = p.bbox || {}
  const s = scale.value
  const kx = b.pw ? it.w / b.pw : 1
  const ky = b.ph ? it.h / b.ph : 1
  return { x: (b.x0 || 0) * kx * s, y: (b.y0 || 0) * ky * s,
           w: ((b.x1 || 0) - (b.x0 || 0)) * kx * s, h: ((b.y1 || 0) - (b.y0 || 0)) * ky * s }
}
function paraStyleOn(p, it) {
  const r = paraBoxOn(p, it)
  return { left: r.x + 'px', top: r.y + 'px', width: r.w + 'px', height: r.h + 'px' }
}
/* 同一页可能同时挂在两项上（双语的原文半 + 译文半）：高亮两半一起亮，
   用户一眼看到"这段在译文里的哪儿"。 */
function itemsOfPage(pno) {
  return flatItems.value.filter(it => paraPageOf(it) === pno)
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
  // 只量自己窗格的批注：多窗格下 document 全局查会把别家窗格的同 id 卡也量一遍，
  // 白量还互相触发响应式更新
  for (const el of (deskEl.value?.querySelectorAll('.mg-note[data-nid]') || [])) {
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
      const p = byIdx.value[pendingPara.value]
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
    const p = byIdx.value[n.para_idx]
    anchorCache.set(k, (p && n.quote && sentenceAround(p.text, n.quote)) || n.quote || '')
  }
  return anchorCache.get(k)
}
function quoteSpan(n) {
  if (!n) return null
  const k = n.id + '|' + n.para_idx
  if (!spanCache.has(k)) {
    const p = byIdx.value[n.para_idx]
    const txt = anchorText(n)
    spanCache.set(k, txt && p ? lineSpanOf(p, txt) : null)
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
    const p = byIdx.value[n.para_idx]
    const pb = p ? paraBoxOn(p, it) : null
    const box = pb ? { y0: pb.y, y1: pb.y + pb.h } : null
    const r = findQuoteRects(el, anchorText(n), box)
    if (r) { out[n.id] = r.rects; cov[n.id] = r.coverage }
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

/* 页边卡的标签：你自己钉的那几种各有名字，前面加"你 · "——
   页边只有你自己的东西，一眼知道这句是谁写的。 */
function kindLabel(n) {
  const zh = kindZH(n)
  return zh ? t('你 · ') + zh : zh
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
  return (byIdx.value[n.para_idx]?.bbox.y0 || 0) * scale.value
}
const currentHit = computed(() => searchHits.value[searchAt.value] || null)
/* 查找范围＝当前版面，占位文案把这层语义说出来：原文/译文各自一边，双语两边都在 */
const searchPlaceholder = computed(() => {
  ui.lang                                   // 语言切换要跟着重算（t() 本身不惹响应式）
  return t(store.viewer.variant === 'mono' ? '在译文里找…'
    : store.viewer.variant === 'dual' ? '在论文里找…' : '在原文里找…')
})
/* 命中按**这一项**（窗格里的那一页）归组，不按页号：译文/双语里译文页的 origPage 是 -1，
   按页号归组的话它们彼此不分、还会把框画到原文页上。gi 就是 flatItems 的下标，
   每一项唯一——双向都对得上。 */
const searchHitsByGi = computed(() => {
  const m = new Map()
  for (const h of searchHits.value) {
    if (!m.has(h.gi)) m.set(h.gi, [])
    m.get(h.gi).push(h)
  }
  return m
})
function searchHitsOnItem(it) { return searchHitsByGi.value.get(it.gi) || [] }

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
    if (!zh.trim()) { toast(t('模型没返回内容')); return }
    const p = paras.value.find(x => x.idx === idx)   // 本窗格自持的段落表：全局 paraByIdx 可能已是别篇的
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
  if (!it) { sel.visible = false; return }   // 选区在别的窗格的纸上：这边开气泡，钉下去就张冠李戴了
  let context = '', paraIdx = -1, page = it ? origPageOf(it) : 0
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

/* 纸上那条划线点一下要有回应。为什么不直接在 .mg-mark 上挂 click：那些块盖在正文上
   （样式里是 pointer-events: none），一旦吃鼠标事件就没法选字了。所以走"整页 mouseup +
   命中测试"——点一下（没拖动）本来也会触发 mouseup，选字、划词一条都不受影响。 */
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
    el.scrollIntoView({ block: 'nearest', behavior: scrollBehavior() })
  })
}

let selStream = null       // 进行中的划词翻译：换选区/关气泡时要掐掉，别让它往已关的气泡里写字
let selSeq = 0             // 每发起一条流 +1：busy 的收尾只认最新那条（被顶掉的老流不许收尾）
function closeSel() {
  selStream?.abort()
  sel.visible = false
  mine.open = false        // 手写批注的草稿框跟着气泡一起收，下次划词不该还开着
}

async function doTranslateSel() {
  selStream?.abort()
  const mine = ++selSeq
  sel.busy = true; sel.err = ''; sel.zh = ''; sel.hits = []
  try {
    // 字先攒进缓冲、按帧合并上屏（照抄 AskPanel 的做法）：每个 SSE 块都直写状态的话，
    // watch 会跟着每个块强制 refitPop 读一次布局，长译文流式时气泡抖、全页掉帧
    const fb = frameBuffer(piece => { sel.zh += piece })
    /* 流对象要留下（selStream）：换选区/关气泡/再翻译时的 abort() 全指着它——
       不存的话那几个 abort 是空转的，旧流会一直往同一个 sel 对象上追加，
       两条流的字混进一个气泡（2026-10-11 审计实锤：selStream 声明后从未赋值） */
    selStream = translateStream(props.pid, 'selection', { text: sel.text, context: sel.context }, ev => {
      if (ev.type === 'delta') fb.push(ev.text)
      else if (ev.type === 'done') { fb.flush(); sel.hits = ev.hits || [] }
      else if (ev.type === 'error') { sel.err = ev.message; if (!sel.zh) sel.zh = '⚠ ' + ev.message }
    })
    await selStream.done
  } catch (e) {
    if (e.name !== 'AbortError') { sel.err = e.message; if (!sel.zh) sel.zh = '⚠ ' + e.message }
  }
  if (mine === selSeq) sel.busy = false    // 老流被 abort 后也走到这里：busy 归新流管，别提前放开
}

let pinning = false
async function pinSel() {
  if (pinning) return               // 双击会 abort 掉第一条流、拿半截译文钉出两张卡
  pinning = true
  try {
    if (!sel.zh || sel.busy) await doTranslateSel()   // 流式版：busy 时也会把流等完
    if (!sel.zh) return
    try {
      await api.pin(props.pid, { quote: sel.text.slice(0, 150), note: sel.zh, para_idx: sel.paraIdx, page: sel.page })
    } catch (e) { toast(t('钉页边失败：{m}', { m: e.message })); return }
    try { await refreshM() } catch { toast(t('页边卡没刷新，重开这篇')) }
    closeSel()
  } finally { pinning = false }
}

async function copySel() {
  await copyWithToast(sel.zh || '', t('已复制'))
}
function sendToGlossary() {
  store.glossaryPrefill = { term_en: sel.text.slice(0, 80), term_zh: (sel.zh || '').replace('〔演示译文〕', '').slice(0, 24) }
  closeSel()
  window.dispatchEvent(new CustomEvent('eggpaper:terms-prefill'))
}

function askAboutSel() {
  store.askPrefill = { text: sel.text.slice(0, 80) }
  closeSel()
}

/* 自己写一条批注：页边不只是"看批注的地方"，也是读者自己的本子。
   查译/段译是"AI 给你的"，这条是你自己的话——所以 kind='note'，标签带「你 · 」前缀，
   笔触与其它钉子同一种墨色（页边只有一种笔触，见 styles.css）。 */
const mine = reactive({ open: false, text: '' })
const mineEl = ref(null)
function openMine() {
  mine.open = true
  mine.text = ''
  focusSoon(() => mineEl.value)
}
let savingMine = false
async function saveMine() {
  /* 局部量别叫 t：i18n 的 t() 就在同一个词法域里，名字一遮，下面 catch 里那句
     t('没写上：{m}') 就变成"对字符串调用函数"——钉页边失败时永远只弹全局兜底提示 */
  const txt = mine.text.trim()
  if (!txt || savingMine) return    // api.pin 在途时文本还没清空，连点会钉两条一样的
  savingMine = true
  try {
    await api.pin(props.pid, { quote: sel.text.slice(0, 150), note: txt, para_idx: sel.paraIdx,
                               page: sel.page, kind: 'note' })
  } catch (e) { toast(t('没写上：{m}', { m: e.message })); return }
  finally { savingMine = false }
  mine.open = false; mine.text = ''
  try { await refreshM() } catch { toast(t('页边卡没刷新，重开这篇')) }
  closeSel()
}

async function refreshM() {
  const m = await api.marginalia(props.pid)
  mnotes.value = m.notes || []
  if (props.pid === store.currentId) Object.assign(store.marginalia, m, { pid: props.pid })
}

async function unpin(n) {
  // 自己写的话删了就没了，问一声；查译/框选这类卡重划一次就有，随手删不用拦
  if (n.kind === 'note') {
    const yes = await confirmBox({ title: t('删这条批注？'), ok: t('删除'), danger: true,
                                   body: (n.note || '').slice(0, 80) })
    if (!yes) return
  }
  try {
    await api.unpin(props.pid, n.id)
  } catch (e) { toast(t('没删成：{m}', { m: e.message })); return }
  try { await refreshM() } catch { toast(t('页边卡没刷新，重开这篇')) }
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

/* 用户主动导航（页码跳页 / 引文段落跳转 / j·k 步进 / Alt+← 回跳——一切主动写滚动
   位置的操作都要报备）：防抖中的自动回位（applyAnchor / reflow 回调 / load 收尾）
   一律让位——否则晚到的旧锚点会把刚跳过去的人拉回去（2026-10-26 跳页被弹回顶部
   就是 reflow 回调晚于跳页 landing；onUserScroll 只认滚轮，认不出程序化滚动）。 */
function markUserNav() { anchorCancel = true }

function scrollToY(y, instant = false) {
  markUserNav()
  const sc = scroller()
  if (!sc) return
  const far = Math.abs(y - sc.scrollTop) > sc.clientHeight * 1.5
  sc.scrollTo({ top: y, behavior: instant || far ? 'auto' : 'smooth' })
}

let searchSeq = 0        // 重入守卫：整本分片扫描很慢，旧一轮回来时不能盖掉新结果
async function runSearch() {
  const q = searchQ.value.trim()
  if (q.length < 2) {
    // 清空查询词：在途的那一轮扫描也要作废（bump seq），不然它跑完照样把命中写回来、
    // 还把视野跳到某个命中上——查找条已经空了，页上却冒出一堆高亮框
    searchSeq++
    searchHits.value = []; searchAt.value = -1; lastSearchQ = ''
    searchBusy.value = false
    return
  }
  const seq = ++searchSeq
  searchBusy.value = true
  const prev = lastSearchQ === q && searchAt.value >= 0 ? searchHits.value[searchAt.value] : null
  const hits = []
  const items = flatItems.value
  /* 查找范围＝**当前版面**看得见的文字：原文版面查原文的文字层、译文版面查译文、
     双语版面两边都查。以前这里跳过了 origPage < 0 的项（译文页整类），于是译文里
     中英文都查不到；现在逐项扫各自渲染出的文字层，版面是哪种就查哪种。
     大部头里带外页面只建了文字层、没画位图（见 renderAll），这里的扫描照样整本覆盖。 */
  await fillTextLayers()                 // 万一带外文字层被新一轮渲染中途打断：补上再扫
  if (seq !== searchSeq) return
  for (let idx = 0; idx < items.length; idx++) {
    const it = items[idx]
    const el = pageEls.value[it.gi]
    if (!el?.querySelector('.textLayer')) continue
    for (const h of findAllRects(el, q)) hits.push({ ...h, gi: it.gi, k: scale.value })
    if ((idx & 15) === 15) await new Promise(r => setTimeout(r, 0))   // 大部头分片扫描：每 16 页让出主线程，别把输入卡死
  }
  if (seq !== searchSeq) return          // 期间又搜了一轮：这份结果过期，丢掉
  hits.sort((a, b) => (a.gi - b.gi) || (a.y - b.y))
  searchHits.value = hits
  lastSearchQ = q
  let at = -1
  if (hits.length) {
    at = 0
    if (prev) {
      const oldY = prev.rects[0].y * (scale.value / prev.k)
      let best = Infinity
      for (let i = 0; i < hits.length; i++) {
        const d = Math.abs(hits[i].gi - prev.gi) * 1e4 + Math.abs(hits[i].rects[0].y - oldY)
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
  const el = pageEls.value[h.gi]
  if (!el) return
  scrollToY(el.offsetTop + h.rects[0].y * hitK(h) - scroller().clientHeight * 0.3, true)
}
function searchStep(d) { if (searchHits.value.length) gotoHit(searchAt.value + d) }
function closeSearch() {
  searchSeq++            // 同上：关掉查找条要作废在途扫描——它跑完会跳视野、画高亮
  searchBusy.value = false
  searchOpen.value = false; searchQ.value = ''; searchHits.value = []; searchAt.value = -1; lastSearchQ = ''
}
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
      if (y0 <= j.y0 * scale.value + 6 && y1 >= j.y0 * scale.value - 6) {
        // 双语版面里同一页挂着两项（原文半 + 译文半）：两半一起亮
        flash.value = { idx: p.idx, gi: it.gi, gis: itemsOfPage(j.page).map(x => x.gi) }
        break
      }
    }
  }
  setTimeout(() => (flash.value = null), 2400)     // 2400 > 入场 180 + 停留 1600 + 淡出 520
  backChip.value = true
  clearTimeout(applyJump._t)
  applyJump._t = setTimeout(() => (backChip.value = false), 3000)
}

/* 页边卡上的引文：点一下跳到纸上那句话，位置就是划线的位置 */
function jumpQuote(n) {
  const boxes = markBoxes(n)
  const it = pageItem(n.page)
  if (!it) return
  const y = boxes.length ? boxes[0].y / scale.value : (n.rect?.y0 ?? byIdx.value[n.para_idx]?.bbox.y0 ?? 0)
  // 译文版面：划线是按**原文文字层**算出来的，搬到译文页上只会画在错处——
  // 退回按段区高亮（paraBoxOn 会按页内比例换算）
  const onOrig = it.origPage >= 0
  store.jump = { pid: props.pid, page: n.page, y0: y, y1: y + 1,
                 rects: onOrig ? boxes : [], at: Date.now() }
}

function jumpBack() {
  const top = backStack.pop()
  if (top != null) {
    markUserNav()   // 用户主动回跳（见 markUserNav 注）
    scroller()?.scrollTo({ top, behavior: scrollBehavior() })   // 卸载后迟到的调用：没有滚动容器就作罢
    backChip.value = false
    return
  }
  // 同篇栈空：看有没有跨篇跳转的迁出点（引用卡/提问引用/互引列表跳过来的）——
  // 开回原篇，位置记忆自动落回离开时的那一行
  const home = store.crossPaperBack
  if (home && home !== props.pid) {
    store.crossPaperBack = null
    store.activatePaper(home)
  }
}

function paraAbsY(idx) {
  const p = byIdx.value[idx]
  if (!p) return null
  const it = pageItem(p.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return null
  return el.offsetTop + paraBoxOn(p, it).y      // 译文版面按页内比例换算（paraBoxOn 的口径）
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
  markUserNav()   // j/k 也是用户主动导航（见 markUserNav 注）
  backStack.push(scroller().scrollTop)
  scroller().scrollTo({ top: y - scroller().clientHeight * 0.3, behavior: scrollBehavior() })
}

async function translateCurrent() {
  if (store.readingPara) await translateParaAndPin(store.readingPara)
  else toast(t('先滚到要译的段落'))
}

/* ---------------- 截图：拖拽框选，选区直接成图（Windows 截图的手感） ----------------
   进入模式后盖全屏暗罩、十字光标；松手先撤罩、等屏幕恢复原样这一拍再抓窗口
   物理像素（后端按前端量好的镶边裁成纯视口），前端按选区 × devicePixelRatio
   裁剪——所见即所得，缩放屏上天然比显示分辨率高一档。剪贴板是底线，落盘看设置。 */
const shotMode = ref(false)
const shotSel = ref(null)
const shotBusy = ref(false)
let shotDrag = null        // 进行中的拖拽监听：收罩（Esc/失焦/卸载）时要连它们一起拆
function shotDragEnd() {
  if (!shotDrag) return
  document.removeEventListener('mousemove', shotDrag.move)
  document.removeEventListener('mouseup', shotDrag.up)
  window.removeEventListener('blur', shotDrag.cancel)
  shotDrag = null
}
function startShot() {
  if (shotBusy.value || shotMode.value) return
  shotMode.value = true
  window.addEventListener('keydown', shotEsc, true)
}
function stopShot() {
  /* 拖到一半按 Esc：暗罩收了，但文档上的 mousemove/mouseup 还挂着——用户以为
     取消了、随手松开鼠标，那个 up 照样按 ≥8px 的选区去截图（还写剪贴板/落盘）。
     所以收罩必须把这一次拖拽的监听一起拆掉。 */
  shotDragEnd()
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
    shotDragEnd()
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
  shotDrag = { move, up, cancel }
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
      name = (await api.screenshotSave(paperName(paperMeta.value), pageNum.value, b64)).name
    }
    toast(name ? t('已复制 · 已保存 {n}', { n: name }) : t('已复制'))
  } catch (e) {
    toast(t('截图失败：{m}', { m: e.message }))
  } finally { shotBusy.value = false }
}

/* 译文在盘上被重写了（重新全文翻译完成）：作废内存里那份 pdf.js 文档，正在看译文/双语
   就地按新成品重取。url 是固定的 `/pdf?variant=mono`，不一并作废的话 pdf.js 永远不会
   再回盘取——纸面会一直停在上一次的译文（2026-10-14 用户反馈的"重译完还是重影"）。
   不动原文（源文件没变），也不强切变体（用户在看什么就还是什么）。 */
function refreshTranslation() {
  dropPaperDocs(props.pid)
  if (store.viewer.variant === 'mono' || store.viewer.variant === 'dual') load()
}

const viewerApiObj = { step, translateCurrent, startShot, jumpBack, translateSelectionKey, stepPage, gotoPage, openSearch, findInPaper, refreshTranslation }
watch(() => props.pid === store.currentId, act => { if (act) store.viewerApi = viewerApiObj }, { immediate: true })

function openSearch() { searchOpen.value = true }
/* 别的面板（术语表/一眼卡关键词）说"去文中找这个词"：预填 + 打开 + 自动搜。
   术语和关键词都是**英文**（取自原文），所以：
   - 原文版面：直接搜。
   - 双语版面：直接搜——原文那半就摆在眼前，不用把人从双语里拽出去。
   - 译文版面：切回原文再搜（译文页里通常没有这几个词，原地搜只会看到"无结果"）。 */
let pendingFind = null       // 等这一轮渲染完再搜（换模式要重新出图、建文字层）
function findInPaper(q) {
  if (!q) return
  if (store.viewer.variant === 'mono') {
    store.viewer.variant = 'original'
    pendingFind = q          // 换模式要重新出图、重建文字层：等 load() 收尾再搜
    return
  }
  if (store.viewer.frame) {
    store.viewer.frame = false        // 文字层只是被 tl-off（visibility）藏住，这一拍就恢复
    nextTick(() => { searchQ.value = q; searchOpen.value = true; runSearch() })
    return
  }
  searchQ.value = q
  searchOpen.value = true
}

/* ---------------- 滚动：scroll-spy + 位置记忆 ---------------- */

let spyT = null, saveT = null
/* 当前是第几页：二分找"最后一个 offsetTop ≤ top"的项。原实现每收到一个 scroll
   事件就从第 0 项线性走到当前页——滚到第 500 页就是每个事件 500 次版面读数。
   页元素的 offsetTop 随文档顺序非递减（对开是左右同高，同高取靠后那项，与线性
   扫描同解），所以二分只花 log(n) 次读数。 */
function pageIdxAtTop(top) {
  const items = flatItems.value
  let lo = 0, hi = items.length - 1, ans = 0
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const el = pageEls.value[items[mid].gi]
    // 二分的前提是"每一项都挂着页元素"（sheets 全量渲染，正常都成立）。撞上没挂的
    // （布局切换的中途）就退回线性扫描：线性那版的语义是"跳过没挂的项"，与二分不同解
    if (!el) return pageIdxLinear(top)
    if (el.offsetTop <= top) { ans = mid; lo = mid + 1 } else hi = mid - 1
  }
  return ans
}
function pageIdxLinear(top) {
  const items = flatItems.value
  let cur = 0
  for (let i = 0; i < items.length; i++) {
    const el = pageEls.value[items[i].gi]
    if (el && el.offsetTop <= top) cur = i
    else if (el) break
  }
  return cur
}
/* "原文页码 → 页元素"索引：与 pageItem 同一套优先级（先认 origPage，再退到
   mono），一次建表、整趟段落 spy 复用。存的是**项**而不是元素：
   段落 y 要按这一项的页高做页内比例换算（paraBoxOn） */
function pageItemIndexOf() {
  const m = new Map()
  for (const it of flatItems.value) {
    if (pageEls.value[it.gi] && it.origPage >= 0 && !m.has(it.origPage)) m.set(it.origPage, it)
  }
  const fallback = (key, it) => { if (!m.has(key)) m.set(key, it) }
  for (const it of flatItems.value) {
    if (!pageEls.value[it.gi] || it.origPage >= 0) continue
    if (it.doc === 'mono') fallback(it.page, it)
  }
  return m
}
function onScroll() {
  updateProg()
  const sc = scroller()
  if (!sc) return          // 卸载后残留的定时器（setTimeout(onScroll, 800) / spyT）还会打一枪
  const items = flatItems.value
  if (items.length) {
    const cur = origPageOf(items[pageIdxAtTop(sc.scrollTop + 8)]) + 1
    if (cur !== pageNum.value) {
      pageNum.value = cur
      if (document.activeElement !== pageInputEl.value) pageIn.value = String(cur)
    }
  }
  clearTimeout(spyT)
  spyT = setTimeout(() => {
    ensureNear()             // 位图被收掉的页滚回来了：视口附近的先补上（小文档这步是空转）
    const focusY = sc.scrollTop + sc.clientHeight * 0.4
    /* 段落 spy：原实现每段都调 paraAbsY，而 paraAbsY 每次都在 flatItems 上线性找三遍页
       ——600 页 / 600 段一次滚动停顿就是几十万次迭代。这里建表 + 每张页只读一次
       offsetTop，再按"段落本就按文档顺序排（idx = 页序）"提前收尾：越过焦点后只会更远。
       段落 y 一律走 paraBoxOn：译文/双语版面里页元素是译文页，按页内比例换算才对得上 */
    const itOf = pageItemIndexOf()
    const topOf = new Map()
    let best = null, bestD = Infinity
    for (const p of paras.value) {
      const it = itOf.get(p.page)
      if (!it) continue
      let base = topOf.get(it)
      if (base === undefined) { base = pageEls.value[it.gi].offsetTop; topOf.set(it, base) }
      const d = Math.abs(base + paraBoxOn(p, it).y - focusY)
      if (d < bestD) { bestD = d; best = p.idx }
      else break
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
  // load 可能横跨整个窗格重组（同屏阅读把旧实例卸掉）：恢复时已卸载就到此为止，
  // 否则下面 deskEl/scroller 全是 null，监听挂不上去还炸一屏
  if (!deskEl.value || !scroller()) return
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
  stopShot()   // 截图/框选模式中窗格被关掉：window 上的 capture 监听必须跟着摘，否则常驻泄漏
  frameDragStop?.(); frameDragStop = null
  if (store.viewerApi === viewerApiObj) store.viewerApi = null   // 活动窗格卸载了，快捷键别再打进来
  docClaims.delete(claimTok)   // 不撤的话，这窗格用过的 doc 会被 LRU 逐出循环永久豁免
  selStream?.abort()
  clearTimeout(spyT); clearTimeout(saveT); clearTimeout(scheduleRender._t)
  clearTimeout(focusNote._t); clearTimeout(applyJump._t); clearTimeout(retryJump._t)
  clearTimeout(reflowT); clearTimeout(searchT)
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
let frameDragStop = null      // 框选拖拽的摘监听器（见 startFrameDrag）

function cropItem(it, r) {
  const canvas = canvases.value[it.gi]
  if (!canvas) return          // 窗格卸载与 mouseup 的竞态：画布已经不在了就没什么可裁
  const dpr = canvas.width / parseFloat(canvas.style.width)
  const sx = r.x0 * dpr, sy = r.y0 * dpr, sw = (r.x1 - r.x0) * dpr, sh = (r.y1 - r.y0) * dpr
  const off = document.createElement('canvas')
  off.width = Math.max(2, sw); off.height = Math.max(2, sh)
  off.getContext('2d').drawImage(canvas, sx, sy, sw, sh, 0, 0, off.width, off.height)
  /* 裁出来的是设备像素（DPR 倍）。气泡里要按 CSS 尺寸摆（1:1），不然小选区被
     拉大 1.4 倍、框沿正好切在字中间，看着像渲染坏了 */
  return { url: off.toDataURL('image/jpeg', 0.85), cssW: Math.round(r.x1 - r.x0) }
}

const vis = reactive({ visible: false, x: 0, y: 0, img: '', imgW: 0, question: '', answer: '', busy: false, err: '',
                       page: 0, paraIdx: 0, rect: null })

/* 气泡长高不出屏的钳位（见 refitPop 注释）。写在三个 reactive 都声明之后。 */
watch([() => sel.visible, () => sel.zh, () => sel.busy, () => sel.err, () => mine.open],
  () => nextTick(() => refitPop(selEl.value, sel)))
watch([() => vis.visible, () => vis.answer, () => vis.busy, () => vis.err],
  () => nextTick(() => refitPop(visEl.value, vis)))

function startFrameDrag(e, it) {
  if (!store.viewer.frame || e.button !== 0) return
  if (it.origPage < 0) { toast(t('切回原文再框选')); return }
  e.preventDefault()
  const el = pageEls.value[it.gi]
  const base = el.getBoundingClientRect()
  const pt = ev => ({ x: ev.clientX - base.left, y: ev.clientY - base.top })
  const p0 = pt(e)
  const move = ev => { const p = pt(ev); frameRect.value = { x0: p0.x, y0: p0.y, x1: p.x, y1: p.y, gi: it.gi } }
  /* stop 提升到实例作用域：拖拽途中窗格被卸载（关窗格/切同屏）时，卸载钩子要能把
     document 上的监听一起摘掉——只存在闭包里的话没人够得着，监听会常驻泄漏，
     还会继续往已卸载的实例里写 frameRect */
  frameDragStop = () => {
    document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up)
    window.removeEventListener('blur', stop)
  }
  const stop = () => frameDragStop?.()
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
    visSeq++    // 新选区顶掉旧的：在途的上一单答案别再写回来
    Object.assign(vis, { visible: true, x: Math.min(window.innerWidth - 410, ev.clientX + 12),
                         y: Math.min(window.innerHeight - 340, Math.max(64, ev.clientY - 60)),
                         img: img.url, imgW: img.cssW, question: t('解释选区内容。'), answer: '', busy: false, err: '',
                         page: it.origPage, paraIdx,
                         rect: it.origPage >= 0 ? { x0: x0 / s, y0: y0 / s, x1: x1 / s, y1: y1 / s } : null })
  }
  frameRect.value = { x0: p0.x, y0: p0.y, x1: p0.x, y1: p0.y }
  document.addEventListener('mousemove', move); document.addEventListener('mouseup', up)
  window.addEventListener('blur', stop)
}

let visSeq = 0   // 框选气泡的代次：旧选区的答案回来时人已换到新选区/气泡已关，写进去就是张冠李戴
function closeVis() { visSeq++; vis.busy = false; vis.visible = false }

async function askVisual() {
  if (!vis.question.trim() || vis.busy) return
  const my = ++visSeq
  vis.busy = true; vis.err = ''
  try {
    const r = await api.askVisual({ image: vis.img, question: vis.question })
    if (my !== visSeq) return            // 期间换过选区/关过气泡：答案别落错地方
    vis.answer = r.answer
  } catch (e) { if (my === visSeq) vis.err = e.message }
  if (my === visSeq) vis.busy = false
}

/* 「分析这张图/这个公式/这张表」三个按钮只预填不发：用户多半要改两句再问（Enter 发送）。
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

let pinningVis = false
async function pinVisual() {
  if (vis.busy || pinningVis) return    // 双击：两条 pin 都带 rect，后端按 region 插入不去重，页边出两张一样的卡
  pinningVis = true
  try {
    if (!vis.answer) await askVisual()
    if (!vis.answer) return
    try {
      await api.pin(props.pid, {
        quote: '[选区] ' + vis.question.slice(0, 60), note: vis.answer.slice(0, 560),
        para_idx: vis.paraIdx, page: vis.page, rect: vis.rect,
      })
    } catch (e) { toast(t('钉页边失败：{m}', { m: e.message })); return }
    await refreshM()
    closeVis()
  } finally { pinningVis = false }
}

watch(() => store.visPrefill, pf => {
  if (!pf) return
  /* 多窗格：每个窗格（每篇）都挂着一个 PdfViewer，这个 prefill 只该被**活动窗格**
     消费。不判的话先跑到的那个 watch（挂载最早 = 常是旧窗格）会把它吃掉：
     气泡开在别的窗格上、钉下去还拿 B 图的坐标插进 A 篇。 */
  if (props.pid !== store.currentId) return
  visSeq++    // 同上：新气泡顶掉旧的
  Object.assign(vis, { visible: true, x: Math.max(60, Math.floor(midX.value || window.innerWidth / 2) - 190), y: 90,
                       img: pf.img, imgW: 0, question: pf.question || t('讲这张图。'), answer: '', busy: false, err: '',
                       page: pf.page ?? 0, paraIdx: pf.paraIdx ?? 0, rect: pf.rect ?? null })
  store.visPrefill = null
  askVisual()
})

/* 点空白处收起浮层；Esc 交给 store.escTick */
function onDocDown(e) {
  const tg = e.target
  if (!tg?.closest) return
  if (vis.visible && !tg.closest('.vis-pop')) closeVis()
  if (sel.visible && !tg.closest('.sel-pop')) closeSel()
}
watch(() => store.escTick, () => {
  closeSel()
  if (vis.visible) closeVis()
  if (searchOpen.value) closeSearch()
})
watch(searchOpen, v => { if (v) focusSoon(() => searchInputEl.value) })
let searchT = null
watch(searchQ, () => { clearTimeout(searchT); searchT = setTimeout(runSearch, 320) })

/* 换版面/换对开姿势：命中框先清掉——里面存的是旧布局的 gi 与页坐标，重排期间
   画出来会落在错的页上。渲染收尾会按新文字层自动重搜（见 renderAll 末尾），
   查询词不动，用户看到的是「结果跟着版面刷新」。 */
watch([() => store.viewer.variant, () => store.viewer.spread],
      () => { searchHits.value = []; searchAt.value = -1; lastSearchQ = ''; doneKeys.clear(); load({ keepPlace: true }); saveLater() })
watch(scale, () => { doneKeys.clear(); doneTxt.clear(); paintGen++; stretchCanvases(); dropFarCanvases(); scheduleRender(); saveLater() })
watch(paras, () => { spanCache.clear(); anchorCache.clear() })
watch(() => store.jump, applyJump)
watch(() => store.viewer.layers, () => measureNotes(), { deep: true })    // 图层开关只动页边卡，画布不用重画
watch(() => store.reflowTick, () => reflow())
watch(mnotes, (n, o) => {
  spanCache.clear(); anchorCache.clear()
  if (n.length && (!o || n.length > o.length)) {
    freshNotes.value = true
    setTimeout(() => (freshNotes.value = false), 1600)
  }
  measureNotes()
})
/* 活动篇的批注由 App 在增删后刷新进全局 store，页边卡跟着全局状态走 */
watch(store.marginalia, m => {
  if (m.pid === props.pid && m.notes) mnotes.value = m.notes
})
/* 页边卷轴出现/消失会改变可用宽度：适宽/适页下要重算一次比例，否则纸面 + 页边
   一起超宽（钉完批注卡片被挤出视口——以前得再点一次「适宽」才回来） */
watch([gutterW, gutterPad], () => { if (fit.value !== 'none' && !loading) reflow() })
</script>

<template>
  <div class="desk-inner" ref="deskEl">
    <Transition name="desk">
      <div class="hatch" v-if="!ready">
        <EggMark class="hatch-mark" />
        <div class="hatch-line"><i :style="{ width: Math.round(loadPct * 100) + '%' }" /></div>
        <div class="hatch-word">{{ t('正在破壳') }}</div>
      </div>
    </Transition>

    <!-- 纸面不跟 ready 走（旧结构是 v-else）：渲染管线要在画布元素**存在**的前提下跑，
         否则整轮空转、靠 hatch 过渡结束 + ResizeObserver 才自愈——后台标签页里
         合成帧暂停、过渡永不结束，纸面就一直是空的。hatch 改成纯覆盖层盖在上面。 -->
    <div class="sheet-stage">
      <div class="spread-row" v-for="(s, si) in sheets" :key="si">
        <div class="page-wrap" v-for="it in s.items" :key="it.key">
            <div class="page" :ref="el => (pageEls[it.gi] = el)"
                 :data-page="it.origPage" :data-scale="scale"
                 :style="{ width: it.w * scale + 'px', height: it.h * scale + 'px' }"
                 @mousedown="e => startFrameDrag(e, it)">

            <canvas :ref="el => (canvases[it.gi] = el)"></canvas>
            <div class="care-wash" v-if="store.viewer.care !== 'off' && !ui.dark"></div>
                        <div class="textLayer" v-if="it.text" :class="{ 'tl-off': store.viewer.frame }"
                 :ref="el => (textLayers[it.gi] = el)"></div>
            <a v-if="!store.viewer.frame" v-for="(lk, li) in docLinks[it.gi] || []" :key="'dl' + li"
               class="doc-link" :href="lk.url" target="_blank" rel="noopener" :title="lk.url"
               :style="{ left: lk.x + 'px', top: lk.y + 'px', width: lk.w + 'px', height: lk.h + 'px' }"
               @click.stop></a>
            <div v-if="frameRect && frameRect.gi === it.gi" class="frame-rect"
                 :style="{ left: Math.min(frameRect.x0, frameRect.x1) + 'px', top: Math.min(frameRect.y0, frameRect.y1) + 'px',
                           width: Math.abs(frameRect.x1 - frameRect.x0) + 'px', height: Math.abs(frameRect.y1 - frameRect.y0) + 'px' }"></div>

            <div class="para-zone">
              <!-- 段区按 paraPageOf(it) 取：原文页与译文页都画（译文页按页内比例落位），
                   双语版面两半各画一次；flash 的 gis 让同页的两半一起亮 -->
              <template v-for="p in parasByPage[paraPageOf(it)] || []" :key="'f' + p.idx">
                <div v-if="flash?.idx === p.idx && (flash.gis || [flash.gi]).includes(it.gi)"
                     class="para-fade hot" :style="paraStyleOn(p, it)"></div>
              </template>

                            <div v-for="(b, bi) in flash?.gi === it.gi ? flash.boxes || [] : []" :key="'fb' + bi"
                   class="para-fade hot boxed" :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              <template v-for="(h, hi) in searchHitsOnItem(it)" :key="'s' + hi">
                <div v-for="(b, bi) in h.rects" :key="bi" class="find-hit" :class="{ cur: h === currentHit }"
                     :style="{ left: b.x * hitK(h) + 'px', top: b.y * hitK(h) + 'px',
                               width: b.w * hitK(h) + 'px', height: b.h * hitK(h) + 'px' }"></div>
              </template>

                            <template v-for="{ n } in notesOnPage(it.origPage)" :key="'n' + n.id">
                <div v-for="(b, bi) in markBoxes(n)" :key="bi" class="mg-mark mine" :data-nid="n.id"
                     :class="{ draw: freshNotes, hot: hotNote === n.id, loose: quoteLoose(n) }"
                     :style="{ left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px' }"></div>
              </template>
            </div>
          </div>

                    <div class="gutter" v-if="it.margin && gutterW > 0"
               :style="{ height: (pageLayouts[it.origPage]?.height || it.h * scale) + 'px',
                         width: gutterW + 'px', marginLeft: gutterPad + 'px' }">
            <div v-for="{ n, top, pending } in notesOnPage(it.origPage)" :key="'mg' + n.id"
                 class="mg-note" :data-nid="n.id"
                 :style="{ top: top + 'px',
                           width: Math.max(120, gutterW - 30) + 'px' }"
                 :class="{ fresh: freshNotes, pending, ping: pingId === n.id,
                           expanded: expandedNote === n.id,
                           allq: openQuote === n.id,
                           clamped: (n.note || '').length > 34, clampable: (n.note || '').length > 34 }"
                 @mouseenter="hoverNote(n.id)" @mouseleave="hoverNote(null)"
                 @click="toggleNote(n)">
              <div class="mg-head">
                <span class="mg-dot"></span>
                <span class="mg-kind">
                  {{ pending ? t('翻译中') : kindLabel(n) }}
                </span>
                <span v-if="(n.note || '').length > 34" class="mg-more">{{ expandedNote === n.id ? t('收起') : t('展开') }}</span>
                                <button v-if="!pending" class="mg-ask" @click.stop="askNotePrefill(n, { openRail: true })">{{ t('问 ↗') }}</button>
                <button v-if="!pending" class="mg-del" @click.stop="unpin(n)">×</button>
              </div>
              <div class="mg-body">{{ prettyChem(n.note) }}</div>
                            <div class="mg-quote-row">
                <span class="mg-quote" :class="{ all: openQuote === n.id }"
                      :title="t('跳到这句：{q}', { q: anchorText(n) })" @click.stop="jumpQuote(n)">“{{ quoteShown(n) }}”</span>
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

        <div class="stage-line" v-if="ready && rendering"><i /></div>

        <div class="read-prog" v-if="ready" :style="{ left: deskRightX + 'px' }"><i :style="{ transform: 'scaleY(' + progPct + ')' }" /></div>

        <Transition name="fade">
    <div v-if="ready" class="desk-float zoom-bar" :style="{ left: midX + 'px', maxWidth: floatMax }"
         v-drag="{ key: 'zoombar' }" data-drag>
      <button :title="t('上一页')" @click="stepPage(-1)">‹</button>
      <span class="zb-page">
        <input type="text" ref="pageInputEl" v-model="pageIn" class="zb-input"
               @keydown.enter="commitPage(); pageInputEl?.blur()" @blur="pageIn = String(pageNum)" />
        <em>/ {{ paperMeta?.n_pages || 0 }}</em>
      </span>
      <button :title="t('下一页')" @click="stepPage(1)">›</button>
      <span class="zb-sep"></span>
      <button :class="{ on: fit === 'width' }" @click="setFit('width')">{{ t('适宽') }}</button>
      <button :class="{ on: fit === 'page' }" @click="setFit('page')">{{ t('适页') }}</button>
      <button class="zb-num" :title="t('实际大小')" @click="setFit('none')">{{ zoomPct }}%</button>
      <button :title="t('缩小')" @click="stepZoom(-1)">－</button>
      <button :title="t('放大')" @click="stepZoom(1)">＋</button>
      <span class="zb-sep"></span>
      <button :class="{ on: searchOpen }" @click="searchOpen = !searchOpen">{{ t('查找') }}</button>
      <button :class="{ on: shotMode }" :disabled="shotBusy" @click="startShot">{{ t('截图') }}</button>
    </div>
    </Transition>

        <Transition name="pop">
    <!-- drag 键升一代（findbar → findbar2）：老记忆里存着"往上拖过"的纵向偏移，
         会让查找条飘在正文中间（用户报过）；换键即把旧位置作废，回到贴着缩放条的默认位 -->
    <div v-if="ready && searchOpen" class="desk-float find-bar" :style="{ left: midX + 'px', maxWidth: floatMax }"
         v-drag="{ key: 'findbar2' }" data-drag>
      <input ref="searchInputEl" v-model="searchQ" class="fb-input" :placeholder="searchPlaceholder"
             @keydown.enter="searchStep(1)" @keydown.escape="closeSearch" />
      <span class="fb-count">
        {{ searchHits.length ? (searchAt + 1) + ' / ' + searchHits.length : (searchBusy ? '…' : t('无结果')) }}
      </span>
      <button :disabled="!searchHits.length" :title="t('上一个')" @click="searchStep(-1)">‹</button>
      <button :disabled="!searchHits.length" :title="t('下一个')" @click="searchStep(1)">›</button>
      <button class="ghost" :title="t('关闭')" @click="closeSearch">×</button>
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
      <button class="ghost sp-x" :title="t('关闭')" @click="closeSel()">×</button>
      <div v-if="sel.busy && !sel.zh" style="font-size:var(--fs-sm);color:var(--ink-3)">{{ t('翻译中…') }}</div>
      <div v-if="sel.err && !sel.zh" style="font-size:var(--fs-sm);color:var(--vermilion)">{{ sel.err }}</div>
      <div class="sp-zh" v-if="sel.zh">{{ sel.zh }}<span v-if="sel.busy" class="qa-caret"></span></div>
      <div class="sp-hits" v-if="sel.hits.length">
        <span class="chip" v-for="h in sel.hits" :key="h.en">📌 {{ h.en }} → {{ h.zh }}</span>
      </div>
            <div class="sp-mine" v-if="mine.open">
        <textarea ref="mineEl" v-model="mine.text" rows="3" :placeholder="t('写点什么…')"
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
            <button style="padding:4px 10px" @click="sendToGlossary">{{ t('收进术语表') }}</button>
          </template>
          <button style="padding:4px 10px" @click="openMine">{{ t('写批注') }}</button>
          <button style="padding:4px 10px" @click="askAboutSel">{{ t('提问') }}</button>
        </template>
      </div>
    </div>
    </Transition>

            <Transition name="pop">
    <div class="sel-pop vis-pop" ref="visEl" v-if="vis.visible" :style="{ left: vis.x + 'px', top: vis.y + 'px' }"
         v-drag="{ key: 'vispop' }" data-drag @mouseup.stop>
      <div class="vp-head">
        <span class="mono-label">{{ t('框选问 AI') }}</span>
        <button class="vp-x" :title="t('关闭')" @click="closeVis">×</button>
      </div>
      <img class="vis-img" :src="vis.img" :style="vis.imgW ? { width: vis.imgW + 'px' } : null" />
      <input ref="visInputEl" type="text" v-model="vis.question" style="width:100%; margin-top:8px"
             @keydown.enter="askVisual" @keydown.escape="closeVis" :placeholder="t('问这个选区…')" />
      <div class="sp-actions">
                <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="visFill(t('分析这张图'))">{{ t('分析这张图') }}</button>
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="visFill(t('分析这个公式'))">{{ t('分析这个公式') }}</button>
        <button style="padding:3px 8px; font-size:var(--fs-sm)" @click="visFill(t('分析这张表'))">{{ t('分析这张表') }}</button>
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
