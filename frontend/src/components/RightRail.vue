<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, store, toast, jumpTo, paraByIdx, ROLE_ZH, ROLE_COLOR, ROLE_TEXT_COLOR, kindColor, kindZH, bandOf,
         paperEpoch, samePaper } from '../store'
import { lineSpanOf, sentenceAround } from '../find'
import { prettyChem } from '../chem'
import AskPanel from './AskPanel.vue'
import MdLite from './MdLite.vue'

/* 眉批只有一个家：纸面页边那些卡。右栏这里曾经还有一份「眉批速览」列表——
   同一批句子在同一页里出现两遍（④里一遍、速览里一遍），删了。 */

const emit = defineEmits(['analyze', 'marginalia'])
const tab = ref('skeleton')
const rbodyEl = ref(null)      // 「↗」指针要滚到指定那一问，得能问到滚动容器

/* 眉批生成中的进度：块数来自服务端（真进度），秒数是本地计时（不依赖服务端时钟）。 */
const marginPct = computed(() => {
  const p = store.marginalia.progress
  if (!p || !p.total) return null
  return Math.max(3, Math.min(100, Math.round((p.done / p.total) * 100)))
})
const marginSecs = ref(0)
let marginT0 = 0
let marginTimer = null
watch(() => store.marginalia.status, (s) => {
  if (s === 'running') {
    marginT0 = Date.now()
    marginSecs.value = 0
    if (!marginTimer) marginTimer = setInterval(() => { marginSecs.value = Math.round((Date.now() - marginT0) / 1000) }, 1000)
  } else if (marginTimer) {
    clearInterval(marginTimer)
    marginTimer = null
  }
})
onUnmounted(() => { if (marginTimer) clearInterval(marginTimer) })
const marginElapsed = computed(() => marginSecs.value)


// 摘一段原文：断在句末更体面，断不了就按字数切
function excerpt(text, cap = 132) {
  const t = String(text || '').replace(/\s+/g, ' ').trim()
  if (t.length <= cap) return t
  const cut = t.slice(0, cap)
  const stop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('。'), cut.lastIndexOf('; '))
  return (stop > cap * 0.55 ? cut.slice(0, stop + 1) : cut + '…')
}

function jumpPara(idx) {
  const p = paraByIdx.value[idx]
  if (p) jumpTo(p.page, p.bbox.y0, p.bbox.y1)
}
// 眉批跳转：跳到这条批注引用的那句话，而不是它所在段的开头。
// 和纸面用同一句话（sentenceAround 补成整句），否则"跳到那句"会跳到半句上
function jumpNote(n) {
  const p = paraByIdx.value[n.para_idx]
  const span = p ? lineSpanOf(p, sentenceAround(p.text, n.quote) || n.quote) : null
  if (span) return jumpTo(n.page, span.bbox.y0, span.bbox.y1)
  if (n.rect) return jumpTo(n.page, n.rect.y0, n.rect.y1)
  jumpPara(n.para_idx)
}

/* ---------- 六个问题（「问题」页签） ----------
   段落角色退到幕后：页边书签、略读蒙纱、点段改判、跳转定位一律照旧，
   但"图例 + 计数"那块 UI 换成读者真正会问的六个问题。
   故意不把答案摊开：问题先出现，点哪条才展开哪条。
   ①③④ 的正文是现成的（缺口段 / 主张链 / 局限段），一分钱不花；
   ②⑤⑥ 要模型写一两句，走「获取」、按篇缓存。 */
const SIX = [
  { k: 'q1', n: 1, q: '要解决什么？' },
  { k: 'q2', n: 2, q: '为什么要解决？', gen: 'why' },
  { k: 'q3', n: 3, q: '怎么解决的？' },
  { k: 'q4', n: 4, q: '还有什么没解决？' },
  { k: 'q5', n: 5, q: '还能做什么？', gen: 'next' },
  { k: 'q6', n: 6, q: '换个学科怎么看？', gen: 'lens' },
]
const openSix = reactive({ q1: false, q2: false, q3: false, q4: false, q5: false, q6: false })
function toggleSix(k) { openSix[k] = !openSix[k] }

const six = reactive({ problem: null, why: null, next: null, lens: null })
const sixBusy = reactive({ problem: false, why: false, next: false, lens: false })
const Q_OF = { problem: 'q1', why: 'q2', next: 'q5', lens: 'q6' }
/* 六问各自的"答没答出来"。免费的 ③④ 看骨架，①②⑤⑥ 看有没有取过。
   有答案的那一问，行首的编号是墨色（没答的是灰的）——不点开也知道哪几问已经落地。 */
const sixHas = computed(() => ({
  q1: !!six.problem?.text, q2: !!six.why?.text,
  q3: !!store.analysis.claims.length, q4: !!(limitParas.value.length + warnNotes.value.length),
  q5: !!six.next?.items?.length, q6: !!six.lens?.items?.length,
}))
const sixMissing = computed(() => ['problem', 'why', 'next', 'lens'].filter(k => !six[k]))
// 六问一次补全：四条答案互不依赖，串行点四次不如一次发出去（各自的加载态还在自己那一问上）
const sixAllBusy = ref(false)
async function genSixAll() {
  const keys = sixMissing.value
  if (!keys.length || sixAllBusy.value) return
  sixAllBusy.value = true
  try { await Promise.all(keys.map(k => genSix(k))) } finally { sixAllBusy.value = false }
}

async function loadSix() {
  Object.assign(six, { problem: null, why: null, next: null, lens: null })
  Object.keys(openSix).forEach(k => (openSix[k] = false))   // 换篇回到"只有问题"的样子
  if (!store.currentId) return
  const mine = paperEpoch()
  try {
    const r = await api.sixAnswers(store.currentId)
    if (!samePaper(mine)) return        // 回来时已经换篇：这是上一篇的答案
    Object.assign(six, r)
  } catch { /* 没缓存很正常 */ }
}
async function genSix(key) {
  if (sixBusy[key]) return
  const mine = paperEpoch()
  sixBusy[key] = true
  try {
    const r = await api.sixAnswer(store.currentId, key)
    if (!samePaper(mine)) return        // 换篇了：别把这答案挂到新论文上
    six[key] = r
    openSix[Q_OF[key]] = true
  } catch (e) { toast(e.message) } finally { sixBusy[key] = false }
}
// 「方法卡」是"怎么解决的"那条的加深版：点一下跳到速览页并顺手取回（没取过才取）
function openMethod() {
  tab.value = 'eye'
  if (!methodCard.value) genMethodCard()
}
/* 一眼卡上的「↗」：指到「问题」页对应的那一问去，展开并滚到它。
   一眼卡负责"三十秒知道这篇讲什么"，想深了顺着箭头走——**指针，不是把内容再抄一遍**。 */
function gotoSix(k) {
  tab.value = 'skeleton'
  openSix[k] = true
  nextTick(() => {
    const i = SIX.findIndex(s => s.k === k)
    const el = rbodyEl.value?.querySelectorAll('.six')[i]
    if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' })
  })
}
// 就一条批注追问：把批注和它引的原话一起交给模型，问题才问得准
function askNote(n) {
  const kind = kindZH(n) || '批注'
  store.askPrefill = {
    question: `眉批标了「${kind}」：「${n.note}」——引文是“${(n.quote || '').slice(0, 60)}”。`
      + `这条判断站得住吗？依据在哪几段？[¶${n.para_idx}]`,
    send: true,
  }
}

// 「去问」：把这一条顺着问下去（带进提问面板并直接发出去）
function askIt(q) {
  if (!q) return
  store.askPrefill = { question: q, send: true }
}

const parasOfRole = roles => store.paras.filter(p => roles.includes(annoRole(p.idx)))
const annoRole = idx => store.analysis.annotations[String(idx)]?.role
const annoOf = idx => store.analysis.annotations[String(idx)] || {}
const limitParas = computed(() => parasOfRole(['limitation']))
/* ④「还有什么没解决」里"眉批标出的可疑之处"：按**档位**收，不按类型名收。
   类型现在是开放词表——模型可以自造「参考态不一」这种 warn 档的批注，
   只认 kind==='warning' 会把它们漏在外面（第四问说的是"读者要当心的"，都属于这条）。 */
const warnNotes = computed(() => store.marginalia.notes.filter(n => bandOf(n) === 'warn'))

/* 页边批注的四个档位开关。四档不是四个色相，是"读的时候给多少注意力"——
   它们同时是纸面上四种笔触，关了就在两边一起消失（纸上、页边各少一批）。
   计数按**全部**批注算（不受开关影响），否则关掉一档就看不到它有几条了。 */
const mnotes = computed(() => store.marginalia.notes)
const BANDS = [
  { k: 'good', zh: '值得读', color: '#1d4e5f' },
  { k: 'warn', zh: '要当心', color: '#b8462e' },
  { k: 'noise', zh: '可跳过', color: '#8e8a80' },
  { k: 'mine', zh: '我写的', color: '#57534a' },
]
const bandCount = computed(() => {
  const m = { good: 0, warn: 0, noise: 0, mine: 0 }
  for (const n of mnotes.value) m[bandOf(n)] = (m[bandOf(n)] || 0) + 1
  return m
})
const bandOn = k => store.viewer.noteBands[k] !== false
const bandAny = computed(() => BANDS.some(b => bandOn(b.k)))
function toggleBand(k) {
  store.viewer.noteBands = { ...store.viewer.noteBands, [k]: !bandOn(k) }
}
// 待解那一行：只报有的那一边。"作者承认 0 处"这种话没人爱看
const todoLine = computed(() => {
  const a = limitParas.value.length, b = warnNotes.value.length
  if (a && b) return `作者承认 ${a} 处局限 · 眉批另标出 ${b} 处可疑`
  if (a) return `作者承认 ${a} 处局限，眉批没有另标可疑`
  if (b) return `眉批标出 ${b} 处可疑，作者自己没写局限`
  return '作者没有明说局限，眉批也没标出可疑之处'
})

function anchorsOf(claim) {
  return claim.anchors
    .map(idx => ({ idx, anno: store.analysis.annotations[String(idx)], para: paraByIdx.value[idx] }))
    .filter(x => x.anno)
}

// ---------- 提问 ----------
// 问答本身在 AskPanel 里（会话、流式、停止、复制、重新生成）；这里只负责
// "该问什么"——论文专属的推荐问题，空态里给出来，省得对着空框发呆。
const GENERIC = ['这篇论文解决什么问题？', '核心结论和最硬的证据是什么？', '方法上有什么可挑剔的地方？', '作者承认了哪些局限？']
const suggest = ref([])
async function loadSuggest() {
  if (!store.currentId || suggest.value.length) return
  try {
    const r = await api.suggest(store.currentId)
    suggest.value = r.questions || []
  } catch { /* 静默，回退到通用问题 */ }
}
const quickList = computed(() => (suggest.value.length ? suggest.value : GENERIC))

// 划词/¶ 提问：切到提问页，剩下的交给 AskPanel（它读 store.askPrefill）
watch(() => store.askPrefill, pf => { if (pf) tab.value = 'ask' })
watch(() => store.askFocusTick, () => { tab.value = 'ask' })
// 提问面板现在常驻（切页签不打断生成），所以"进页面就有光标"要自己补一下
watch(tab, t => { if (t === 'ask') store.askFocusTick++ })

// ---------- 术语 ----------
const terms = ref([])
const termFilter = ref('')
const termForm = ref({ term_en: '', term_zh: '' })

async function loadTerms() {
  terms.value = await api.glossary()
}
async function addTerm() {
  if (!termForm.value.term_en.trim() || !termForm.value.term_zh.trim()) return
  await api.glossaryAdd({ ...termForm.value, source: 'manual' })
  termForm.value = { term_en: '', term_zh: '' }
  loadTerms()
}
async function delTerm(id) {
  await api.glossaryDelete(id)
  loadTerms()
}
function onPrefill() {
  if (store.glossaryPrefill) {
    termForm.value = { ...store.glossaryPrefill }
    tab.value = 'terms'
    store.glossaryPrefill = null
  }
}
onMounted(() => {
  loadTerms()
  window.addEventListener('eggpaper:terms-prefill', onPrefill)
  window.addEventListener('keydown', onFigKey)
})
onUnmounted(() => {
  window.removeEventListener('eggpaper:terms-prefill', onPrefill)
  window.removeEventListener('keydown', onFigKey)
})
watch(tab, t => {
  if (t === 'ask') loadSuggest()
  if (t === 'eye') { loadFigures(); loadCachedBlocks() }
})
// 重算析读 / 重写眉批之后，服务端把由主张派生的缓存都作废了，前端手里那份也得跟着丢，
// 否则一眼卡还是旧的、方法卡还是旧的、三问还在问一句已经删掉的"有坑"。
watch(() => store.analysis.status, s => {
  if (s !== 'done') return
  methodCard.value = null; advisor.value = []; suggest.value = []
  loadSuggest(); loadCachedBlocks()
})
watch(() => store.marginalia.status, s => {
  if (s !== 'done') return
  advisor.value = []
  loadCachedBlocks()
})

/* ---------- 右栏宽度：拖动改，双击复位 ----------
   经典分栏拖动：位移直接写进 railW，宽度过渡由 CSS 负责；拖的过程中把过渡关掉
   （body.rail-resizing），松手再交还给 CSS，否则边缘会黏在手后面。
   键盘也能改（←/→），拖不动鼠标的人不该被挡在外面。 */
const RAIL_MIN = 268, RAIL_MAX = 760, RAIL_DEF = 336
const railDragging = ref(false)
let rStartX = 0, rStartW = 0

function startRailResize(e) {
  e.preventDefault()
  rStartX = e.clientX
  rStartW = store.viewer.railW
  railDragging.value = true
  document.body.classList.add('rail-resizing')
  document.addEventListener('mousemove', onRailResize)
  document.addEventListener('mouseup', endRailResize)
}
function onRailResize(e) {
  const w = rStartW - (e.clientX - rStartX)      // 往左拖 = 变宽
  store.viewer.railW = Math.round(Math.min(RAIL_MAX, Math.max(RAIL_MIN, w)))
}
function endRailResize() {
  railDragging.value = false
  document.body.classList.remove('rail-resizing')
  document.removeEventListener('mousemove', onRailResize)
  document.removeEventListener('mouseup', endRailResize)
  store.reflowTick++          // 拖完了才让论文重新定标，拖的过程中不重排（不然每帧都在重画）
}
function nudgeRail(d) {
  store.viewer.railW = Math.round(Math.min(RAIL_MAX, Math.max(RAIL_MIN, store.viewer.railW + d)))
  store.reflowTick++
}
onUnmounted(() => {
  endRailResize()
})

// ---------- 方法卡 / 缩写 / mini-map ----------
const methodCard = ref(null)
const mcBusy = ref(false)
const mcMore = ref(false)          // 方法卡展开：默认只露前三步
const MC_STEPS = 3
const stepsShown = computed(() => {
  const all = methodCard.value?.steps || []
  return mcMore.value ? all : all.slice(0, MC_STEPS)
})
async function genMethodCard() {
  const mine = paperEpoch()
  mcBusy.value = true
  try {
    const r = await api.methodCard(store.currentId)
    if (!samePaper(mine)) return
    methodCard.value = r
  } catch (e) { toast('生成失败：' + e.message) } finally { mcBusy.value = false }
}
// 本文缩写：**只列还没收进术语表的**。收进去之后它就出现在下面那张表里了，
// 同一对 en→zh 在同一屏里出现两次没有意义（反馈由 toast 负责）。
const abbrList = computed(() => {
  try {
    const abbrs = JSON.parse(store.paper?.abbrs || '{}')
    const saved = new Set(terms.value.map(t => t.term_en.toLowerCase()))
    return Object.entries(abbrs)
      .filter(([en]) => !saved.has(en.toLowerCase()))
      .map(([en, zh]) => ({ en, zh }))
  } catch { return [] }
})
async function saveAbbr(a) {
  await api.glossaryAdd({ term_en: a.en, term_zh: a.zh, source: 'abbr' })
  loadTerms()                     // 列表里少一条、下面的术语表多一条，动作可见
  toast(`「${a.en}」已收进术语表`)
}
function eqq(idx) {
  const m = store.analysis.evidence_qs || {}
  return m[String(idx)] || ''
}

// ---------- 导师三问 ----------
// 不主动预生成：这是要花 token 的一次调用，没点"获取"就不该发生。
// 但**读缓存**是免费的——进速览页时静默读一次，算过的东西就直接显示出来，
// 不然每次换篇都要重点「获取」，点了才知道"其实早算过了"。
const advisor = ref([])
const advBusy = ref(false)
async function loadAdvisor() {
  if (advBusy.value || advisor.value.length) return
  const mine = paperEpoch()
  advBusy.value = true
  try {
    const r = await api.advisor(store.currentId)
    if (!samePaper(mine)) return
    advisor.value = r.questions || []
  } catch (e) { toast('生成失败：' + e.message) } finally { advBusy.value = false }
}
async function loadCachedBlocks() {
  if (!store.currentId) return
  const mine = paperEpoch()
  if (!methodCard.value) {
    try {
      const r = await api.methodCard(store.currentId, true)
      if (samePaper(mine) && r?.goal) methodCard.value = r
    } catch { /* 没缓存很正常 */ }
  }
  if (!advisor.value.length && store.analysis.status === 'done') {
    try {
      const r = await api.advisor(store.currentId, true)
      if (!samePaper(mine)) return
      advisor.value = r.questions || []
    } catch { /* 同上 */ }
  }
}

// ---------- 图表速览 ----------
const figures = ref([])
// 灯箱用序号而不是对象：这样能左右翻图，像看图片一样一张张过
const figIdx = ref(-1)
const lightbox = computed(() => (figIdx.value >= 0 ? figures.value[figIdx.value] || null : null))
function figStep(d) {
  if (figures.value.length < 2) return
  figIdx.value = (figIdx.value + d + figures.value.length) % figures.value.length
}
function onFigKey(e) {
  if (figIdx.value < 0) return
  if (e.key === 'ArrowLeft') { figStep(-1); e.preventDefault() }
  else if (e.key === 'ArrowRight') { figStep(1); e.preventDefault() }
  else if (e.key === 'Escape') figIdx.value = -1
}
async function loadFigures() {
  if (!store.currentId || figures.value.length) return
  try {
    const r = await api.figures(store.currentId)
    figures.value = r.figures || []
  } catch { /* 无图论文静默 */ }
}
async function askFigure(f) {
  try {
    const url = api.figureUrl(store.currentId, f, 150)
    const blob = await (await fetch(url)).blob()
    const img = await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob) })
    figIdx.value = -1
    const para = store.paras.find(p => p.page === f.page && p.bbox.y0 <= f.y1 && p.bbox.y1 >= f.y0)
    store.visPrefill = {
      img, question: '讲解这张图：画了什么、支持论文的哪个结论、有什么可疑之处。',
      page: f.page, paraIdx: para?.idx ?? 0, rect: { x0: f.x0, y0: f.y0, x1: f.x1, y1: f.y1 },
    }
  } catch (e) { toast('取图失败：' + e.message) }
}
function figJump(f) {
  jumpTo(f.page, f.y0, f.y1)
  figIdx.value = -1
}

const termsFiltered = computed(() => {
  const f = termFilter.value.trim().toLowerCase()
  const list = [...terms.value].sort((a, b) => a.term_en.localeCompare(b.term_en))
  if (!f) return list
  return list.filter(t => t.term_en.toLowerCase().includes(f) || t.term_zh.includes(f))
})
// 「文中」：把术语和原文连起来——搜索框预填这个英文词，第一条命中直接跳过去。
// 从前术语表是个孤岛：知道译法，却没法回原文看它到底怎么用的。
function findTerm(en) {
  if (!en) return
  store.viewerApi?.findInPaper(en)
  toast(`在文中找「${en}」`)
}

/* 换篇：所有"按篇"的东西都要清干净。不清的症状是上一章的方法卡、导师三问、
   图表缩略图、推荐问题在新论文上继续摆着——而这些还都带 `if (已有) return` 的守卫，
   意味着它们**永远不会**被换成新论文的（比闪一下更难发现）。 */
watch(() => store.currentId, () => {
  tab.value = 'skeleton'
  methodCard.value = null
  mcMore.value = false
  advisor.value = []
  figures.value = []
  suggest.value = []
  loadSix()
}, { immediate: true })
</script>

<template>
  <aside class="rail-right">
    <!-- 分栏拖手：贴在右栏左缘，往左拖变宽 -->
    <div class="rail-grip" :class="{ on: railDragging }" role="separator" aria-orientation="vertical"
         tabindex="0" title="拖动改宽度 · 双击复位 · ←→"
         @mousedown="startRailResize" @dblclick="store.viewer.railW = RAIL_DEF; store.reflowTick++"
         @keydown.left.prevent="nudgeRail(28)" @keydown.right.prevent="nudgeRail(-28)"></div>
    <div class="rtabs">
      <button class="rt" :class="{ on: tab === 'skeleton' }" @click="tab = 'skeleton'">问题</button>
      <button class="rt" :class="{ on: tab === 'eye' }" @click="tab = 'eye'">速览</button>
      <button class="rt" :class="{ on: tab === 'ask' }" @click="tab = 'ask'">提问</button>
      <button class="rt" :class="{ on: tab === 'terms' }" @click="tab = 'terms'">术语</button>
      <button class="rt-collapse" title="收起右栏（x）" @click="store.viewer.railUser = false">»</button>
    </div>
    <div class="rbody" ref="rbodyEl" :class="{ flush: tab === 'ask' }">
      <!-- 页签切换：三个静态页共用一层过渡（出去快、进来稍慢），换页时内容是"落定"而不是"啪一下换掉"。
           提问页不在这层里——它必须常驻（切走不能掐断正在生成的回答），单独用下面那个 v-show 层。 -->
      <Transition name="rt" mode="out-in">
      <div class="rt-pane" v-if="tab !== 'ask'" :key="tab">
      <!-- ============ 问题 ============ -->
      <template v-if="tab === 'skeleton'">
        <div class="reading" v-if="store.analysis.status === 'running'">
          <div class="r-line">正在通读<span class="r-dots">…</span></div>
          <div class="r-bar"><i /></div>
        </div>
        <div v-else-if="store.analysis.status === 'error'" style="padding:8px 2px">
          <div style="font-size:var(--fs-sm);color:var(--vermilion);line-height:1.6">{{ store.analysis.error }}</div>
          <button style="margin-top:10px" @click="emit('analyze')">重试</button>
        </div>
        <!-- 没析读时只陈述状态：顶栏那颗「析读」就在上面，同一屏里放第二个同名按钮是重复 -->
        <div v-else-if="store.analysis.status !== 'done'" style="padding:8px 2px">
          <div style="font-size:var(--fs-md);line-height:1.75;color:var(--ink-2)">
            {{ store.paras.length ? '还没有析读——顶栏「析读」读完全文，才有这六个问题的答案。' : '这份 PDF 没有可提取的文字层（多半是扫描件）。原文照样能读，图表也能框选问 AI，但这六个问题答不了。' }}
          </div>
        </div>

        <template v-else-if="!store.paras.length">
          <div class="r-note">这份 PDF 没有可提取的文字层（多半是扫描件）。原文照样能读，图表也能框选问 AI，但这六个问题答不了。<span v-if="figures.length"> 速览页有 {{ figures.length }} 张图可以看。</span></div>
        </template>

        <template v-else>
          <!-- 六个问题：读一篇论文该带着的问题。问题免费、答案点开才看 -->
          <div class="six-head">
            <span class="mono-num" v-if="store.analysis.status === 'done' && sixMissing.length">
              {{ 6 - sixMissing.length }}/6 已有答案
            </span>
            <button v-if="store.analysis.status === 'done' && sixMissing.length > 1"
                    class="six-allget" :disabled="sixAllBusy" title="把还没答案的几问一次取回来（各问各自的加载态）"
                    @click="genSixAll">
              {{ sixAllBusy ? '正在取…' : `补全其余 ${sixMissing.length} 问` }}
            </button>
            <span v-if="store.readingPara" class="mono-num">读至 ¶{{ store.readingPara }} / {{ store.paras.length }}</span>
          </div>

          <section class="six" v-for="s in SIX" :key="s.k" :class="{ open: openSix[s.k] }">
            <button class="six-q" @click="toggleSix(s.k)">
              <i :class="{ on: sixHas[s.k] }" :title="sixHas[s.k] ? '这一问已经有答案' : '还没取过'">{{ s.n }}</i><span class="qt">{{ s.q }}</span>
              <b v-if="s.k === 'q3' && store.analysis.claims.length">{{ store.analysis.claims.length }}</b>
              <b v-else-if="s.k === 'q4' && limitParas.length + warnNotes.length">{{ limitParas.length + warnNotes.length }}</b>
            </button>

            <!-- 展开是"长出来"的，不是"跳出来"的：0fr→1fr 的 grid 过渡才真的在动高度 -->
            <div class="six-fold" :class="{ open: openSix[s.k] }">
             <div class="six-fold-in">
            <div class="six-a">
              <!-- ① 要解决什么：**直接说出来**。原文里没有哪一句现成写着"我们要解决什么"，
                   那是要从引言里综合出来的——所以这一问的答案是模型的一句话，段落只作为依据
                   标在句尾（原文在纸上，点 ¶ 就到，不必在这里再抄一遍）。 -->
              <template v-if="s.k === 'q1'">
                <MdLite v-if="six.problem?.text" class="six-txt" :text="six.problem.text" @cite="jumpPara" />
                <button v-else class="six-get" :disabled="sixBusy.problem" @click="genSix('problem')">
                  {{ sixBusy.problem ? '正在想' : '获取' }}
                </button>
              </template>

              <!-- ② 为什么要解决：生成一句（含依据段号），点右侧获取 -->
              <template v-else-if="s.k === 'q2'">
                <MdLite v-if="six.why?.text" class="six-txt" :text="six.why.text" @cite="jumpPara" />
                <button v-else class="six-get" :disabled="sixBusy.why" @click="genSix('why')">
                  {{ sixBusy.why ? '正在想' : '获取' }}
                </button>
              </template>

              <!-- ③ 怎么解决的：主张 → 证据链 -->
              <template v-else-if="s.k === 'q3'">
                <div class="claim-item" v-for="c in store.analysis.claims" :key="c.id">
                  <div class="c-head" @click="c.anchors.length && jumpPara(c.anchors[0])">
                    <span class="c-id">{{ c.id }}</span>
                    <span class="c-txt">{{ c.text }}</span>
                  </div>
                  <div class="ev-row" v-for="a in anchorsOf(c)" :key="a.idx" @click="jumpPara(a.idx)">
                    <span class="e-dot">¶{{ a.idx }}</span>
                    <span class="e-bar" :style="{ background: ROLE_COLOR[a.anno.role] }"></span>
                    <span class="e-note">
                      <span class="rg-kind" :style="{ color: ROLE_TEXT_COLOR[a.anno.role] }">{{ ROLE_ZH[a.anno.role] }}</span>
                      {{ a.anno.purpose }}
                      <div class="ev-q" v-if="eqq(a.idx)">该实验回答：{{ eqq(a.idx) }}</div>
                    </span>
                  </div>
                  <div v-if="!anchorsOf(c).length" class="six-note">未找到直接证据段</div>
                </div>
                <div class="six-foot">
                  <button @click="openMethod">方法卡 ↗</button>
                </div>
              </template>

              <!-- ④ 还有什么没解决：局限段 + 眉批里标"有坑"的句子 -->
              <template v-else-if="s.k === 'q4'">
                <div v-for="p in limitParas" :key="p.idx" class="gap-node">
                  <div class="gap-row" @click="jumpPara(p.idx)">
                    <span class="g-tag">¶{{ p.idx }}</span>
                    <span class="g-txt">{{ annoOf(p.idx).purpose }}</span>
                  </div>
                  <div class="gap-quote" @click="jumpPara(p.idx)">{{ excerpt(p.text) }}</div>
                </div>
                <div v-for="n in warnNotes" :key="'w' + n.id" class="ev-row" @click="jumpNote(n)">
                  <span class="e-dot">¶{{ n.para_idx }}</span>
                  <span class="e-bar" :style="{ background: kindColor(n) }"></span>
                  <span class="e-note">
                    {{ prettyChem(n.note) }}
                    <button class="ev-ask" title="就这条批注追问模型" @click.stop="askNote(n)">问 ↗</button>
                  </span>
                </div>
                <div class="six-note" v-if="!limitParas.length && !warnNotes.length">
                  作者没有明说局限，眉批里也没有标出可疑之处。
                </div>
              </template>

              <!-- ⑤⑥ 生成型：几条方向 / 几个学科视角，每条都能顺下去问 -->
              <template v-else>
                <div class="six-item" v-for="(it, i) in (six[s.gen]?.items || [])" :key="i">
                  <div class="si-lead" v-if="it.lead">{{ it.lead }}</div>
                  <MdLite class="six-txt" :text="it.text" @cite="jumpPara" />
                  <button class="si-ask" v-if="it.ask" @click="askIt(it.ask)">{{ it.ask }} ↗</button>
                </div>
                <button v-if="!six[s.gen]?.items?.length" class="six-get"
                        :disabled="sixBusy[s.gen]" @click="genSix(s.gen)">
                  {{ sixBusy[s.gen] ? '正在想' : '获取' }}
                </button>
              </template>
            </div>
             </div>
            </div>
          </section>

          <!-- 眉批的家在纸面页边：这里只给"它们在哪儿"、生成入口，和**按档位筛**的开关。
               状态是 error 时结果**还在**（失败不抹旧结果），所以这里说的是"这次的没成"，
               不是"眉批没了"——别让用户以为页边那些批注也作废了。 -->
          <div class="blk">
            <div class="blk-head">
              <span class="mono-label">眉批<span v-if="mnotes.length"> · {{ mnotes.length }}</span></span>
              <button v-if="store.marginalia.status !== 'running'" class="blk-get" @click="emit('marginalia')"
                      :title="mnotes.length ? '重写全文眉批（旧的会被替换）' : '通读全文，在页边写下批注'">
                {{ mnotes.length ? '重写' : 'AI 眉批' }}
              </button>
              <span v-else class="blk-busy">写批注中<span class="r-dots">…</span></span>
            </div>
            <!-- 生成中的真实进度：服务端按"读完几块"回报（12 段一块），不是装饰动画。
                 首次生成要通读全文，长论文十几块，这条线就是"还要等多久"的答案。 -->
            <div class="blk-prog" v-if="store.marginalia.status === 'running'">
              <div class="r-bar">
                <i :class="{ det: marginPct !== null }" :style="marginPct !== null ? { width: marginPct + '%' } : null" />
              </div>
              <div class="blk-prog-line">
                <span v-if="store.marginalia.progress?.total">已读 {{ store.marginalia.progress.done }}/{{ store.marginalia.progress.total }} 块</span>
                <span v-else>正在通读全文</span>
                <span class="blk-elapsed">{{ marginElapsed }}s</span>
              </div>
            </div>
            <!-- 页边按档位筛：四档就是纸上四种笔触。三四十条批注的时候，
                 "只看要当心"是读者的第一个念头；关掉的档位在纸上和页边同时消失。 -->
            <div class="band-bar" v-if="mnotes.length">
              <button v-for="b in BANDS" :key="b.k" class="band-chip" :class="{ off: !bandOn(b.k) }"
                      :title="bandOn(b.k) ? `纸面上显示「${b.zh}」（点一下收起）` : `「${b.zh}」现在收起了（点一下显示）`"
                      @click="toggleBand(b.k)">
                <i class="bdot" :style="{ background: b.color }"></i>{{ b.zh }}<span class="n">{{ bandCount[b.k] }}</span>
              </button>
            </div>
            <div class="band-alloff" v-if="mnotes.length && !bandAny">
              四档都收起了，纸面上没有批注 ·
              <button class="lnk" @click="store.viewer.noteBands = { good: true, warn: true, noise: true, mine: true }">全开</button>
            </div>
            <p class="blk-warn" v-if="store.marginalia.status === 'error' && store.marginalia.error">
              {{ store.marginalia.error }}
            </p>
            <!-- 完成了但有块没生成：页边少了一段，得说出来，否则用户以为那段没问题 -->
            <p class="blk-warn" v-else-if="store.marginalia.error">{{ store.marginalia.error }}</p>
          </div>
        </template>
      </template>

      <!-- ============ 速览 ============ -->
      <!-- 这一页只干三件事：三十秒定位（一眼卡）、能不能复现（方法卡）、组会会被问什么（导师三问）。
           「为什么重要」归问题页②，「依据在哪」归③，「作者承认了什么」归④——这里只留指针，不搬内容。 -->
      <template v-if="tab === 'eye'">
        <div v-if="store.summaryErr" class="r-note">{{ store.summaryErr }}</div>
        <div v-else-if="!store.summary" class="reading">
          <div class="r-line">正在写一眼卡<span class="r-dots">…</span></div>
          <div class="r-bar"><i /></div>
        </div>
        <div class="card-eye" v-else>
          <div class="ce-one">{{ prettyChem(store.summary.one_line) }}</div>
          <div class="ce-row go" @click="gotoSix('q3')" title="去「问题」页第 3 问：主张与证据链">
            <span class="ce-k">发现</span><span class="ce-v">{{ prettyChem(store.summary.findings) }}</span>
            <span class="ce-go">↗</span>
          </div>
          <div class="ce-row go" @click="gotoSix('q4')" :title="`去「问题」页第 4 问：${todoLine}`">
            <span class="ce-k">待解</span>
            <span class="ce-v">{{ todoLine }}</span>
            <span class="ce-go">↗</span>
          </div>
          <div class="ce-kw"><span class="chip" v-for="k in store.summary.keywords" :key="k">{{ k }}</span></div>
        </div>

        <!-- 图表：紧跟着一眼卡。读完结论就想看图，这是读论文的自然顺序 -->
        <div class="blk" v-if="figures.length">
          <div class="blk-head"><span class="mono-label">图表速览 · {{ figures.length }}</span></div>
          <div class="fig-strip">
            <img v-for="(f, i) in figures" :key="i" class="fig-thumb" :src="api.figureUrl(store.currentId, f)"
                 :title="`第 ${f.page + 1} 页`" @click="figIdx = i" />
          </div>
        </div>

        <!-- 方法卡：目标/体系/条件 + 前三步默认露出，其余收起（八步全铺开自己就一屏） -->
        <div class="blk">
          <div class="blk-head">
            <span class="mono-label">方法卡</span>
            <button v-if="!methodCard?.goal && !mcBusy" class="blk-get" @click="genMethodCard"
                    title="把方法整理成可复现的 protocol">获取</button>
            <span v-else-if="mcBusy" class="blk-busy">获取中<span class="r-dots">…</span></span>
          </div>
          <div class="card-eye" v-if="methodCard?.goal">
            <div class="ce-row"><span class="ce-k">目标</span><span class="ce-v">{{ prettyChem(methodCard.goal) }}</span></div>
            <div class="ce-row"><span class="ce-k">体系</span><span class="ce-v">{{ prettyChem(methodCard.system) }}</span></div>
            <div class="ce-row"><span class="ce-k">条件</span><span class="ce-v">{{ prettyChem(methodCard.conditions) }}</span></div>
            <div class="ce-row"><span class="ce-k">步骤</span>
              <span class="ce-v">
                <div class="mc-step" :class="{ unfold: mcMore && i >= MC_STEPS }"
                     :style="mcMore && i >= MC_STEPS ? { animationDelay: (i - MC_STEPS) * 45 + 'ms' } : null"
                     v-for="(s, i) in stepsShown" :key="i">{{ i + 1 }}. {{ prettyChem(s) }}</div>
              </span>
            </div>
            <div class="ce-row" v-if="mcMore && methodCard.notes"><span class="ce-k">注意</span><span class="ce-v">{{ prettyChem(methodCard.notes) }}</span></div>
            <button v-if="!mcMore && methodCard.steps.length > MC_STEPS" class="blk-more" @click="mcMore = true">
              展开全部 {{ methodCard.steps.length }} 步<span v-if="methodCard.notes"> · 注意</span>
            </button>
          </div>
        </div>

        <!-- 导师三问：只问作者没承认的那一层。作者认了的、眉批标了的，在问题页④ -->
        <div class="blk">
          <div class="blk-head">
            <span class="mono-label">导师三问</span>
            <button v-if="!advisor.length && !advBusy && store.analysis.status === 'done'" class="blk-get"
                    @click="loadAdvisor" title="组会 / 答辩时最可能被问住的三个问题">获取</button>
            <span v-else-if="advBusy" class="blk-busy">获取中<span class="r-dots">…</span></span>
          </div>
          <div v-if="advisor.length">
            <div class="adv-item" v-for="(q, i) in advisor" :key="i">
              <div class="adv-q">Q{{ i + 1 }} · {{ prettyChem(q.q) }}</div>
              <ul class="adv-outline"><li v-for="o in q.outline" :key="o">{{ prettyChem(o) }}</li></ul>
            </div>
          </div>
          <!-- 没算过 vs 算不了：这是两件事。以前两者共用一句"先析读全文"，
               已经析读过的论文上就这么明晃晃地显示着一句和事实相反的话。 -->
          <div v-else-if="store.analysis.status === 'done'" class="six-note">
            还没算过——点右上「获取」，会问到作者没承认的那一层。
          </div>
          <div v-else class="six-note">先析读全文，才有主张和薄弱点可以问。</div>
        </div>

        <!-- 导出 -->
        <div class="blk" style="display:flex;gap:8px">
          <a class="exp-btn" :href="api.exportMdUrl(store.currentId)" download>导出笔记 .md</a>
        </div>
      </template>

      <!-- ============ 提问 ============ -->
      <template v-if="tab === 'terms'">
        <!-- 本文用到的缩写：论文自带的，一键收进术语表 -->
        <div style="margin-bottom:14px" v-if="abbrList.length">
          <div class="mono-label" style="margin-bottom:6px">本文缩写 · {{ abbrList.length }}</div>
          <div class="abbr-list">
            <div class="term-row" v-for="a in abbrList" :key="a.en">
              <span class="t-en" :title="a.en">{{ a.en }}</span>
              <span class="t-arrow">→</span>
              <span class="t-zh" :title="a.zh">{{ a.zh }}</span>
              <button class="t-del" style="font-size:var(--fs-sm)" title="收进术语表"
                      @click="saveAbbr(a)">＋</button>
            </div>
          </div>
        </div>

        <div class="mono-label" style="margin-bottom:6px">术语表 · {{ terms.length }}</div>
        <div class="term-form">
          <input type="text" v-model="termForm.term_en" placeholder="英文" />
          <input type="text" v-model="termForm.term_zh" placeholder="中文" />
          <button title="添加" @click="addTerm">＋</button>
        </div>
        <input type="text" v-model="termFilter" placeholder="筛选…" class="term-filter" />
        <div style="margin-bottom:10px"><a class="exp-btn" :href="api.glossaryCsvUrl" download>导出 CSV</a></div>
        <div v-for="t in termsFiltered" :key="t.id" class="term-row">
          <span class="t-en" :title="t.term_en">{{ t.term_en }}</span>
          <span class="t-arrow">→</span>
          <span class="t-zh">{{ t.term_zh }}</span>
          <button class="t-go" title="在论文里找这个词（跳过去、并高亮命中）" @click="findTerm(t.term_en)">文中</button>
          <button class="t-del" @click="delTerm(t.id)" title="删除">×</button>
        </div>
      </template>
      </div>
      </Transition>

      <!-- ============ 提问 ============ -->
      <!-- 常驻不卸载：切去看原文时，正在生成的回答不该被掐掉。unmount 会 abort 掉这条流，
           服务端因此不写回答行——库里的症状就是"只有问题、没有回答"。
           所以改成显示/隐藏，在 rbody 上盖一层。 -->
      <div class="ask-layer" v-show="tab === 'ask'">
        <AskPanel :quick="quickList" />
      </div>
    </div>

    <!-- 图表灯箱：像看图片一样左右翻 -->
    <Transition name="fade">
    <div class="lightbox" v-if="lightbox" @click="figIdx = -1">
      <div class="lb-stage" @click.stop>
        <button class="lb-nav" :disabled="figures.length < 2" title="上一张（←）" @click="figStep(-1)">‹</button>
        <img :src="api.figureUrl(store.currentId, lightbox, 200)" />
        <button class="lb-nav" :disabled="figures.length < 2" title="下一张（→）" @click="figStep(1)">›</button>
      </div>
      <div class="lb-actions" @click.stop>
        <span class="mono-label">{{ figIdx + 1 }} / {{ figures.length }}</span>
        <button @click="figJump(lightbox)">在原文查看</button>
        <button @click="askFigure(lightbox)">问这张图</button>
        <button @click="figIdx = -1">关闭</button>
      </div>
    </div>
    </Transition>
  </aside>
</template>
