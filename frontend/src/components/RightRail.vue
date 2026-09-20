<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, store, toast, jumpTo, jumpPara, askNotePrefill, paraByIdx, ROLE_ZH, ROLE_COLOR, ROLE_TEXT_COLOR, kindColor, kindZH, bandOf,
         paperEpoch, samePaper, reloadSummary } from '../store'
import { useEdgeResize } from '../edgeResize'
import { lineSpanOf, sentenceAround } from '../find'
import { prettyChem } from '../chem'
import { t, isEn } from '../i18n'
import { copyText } from '../clip'
import AskPanel from './AskPanel.vue'
import MdLite from './MdLite.vue'

/* 眉批只有一个家：纸面页边那些卡，右栏只给生成入口与档位开关——
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

function excerpt(text, cap = 132) {
  const t = String(text || '').replace(/\s+/g, ' ').trim()
  if (t.length <= cap) return t
  const cut = t.slice(0, cap)
  const stop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('。'), cut.lastIndexOf('; '))
  return (stop > cap * 0.55 ? cut.slice(0, stop + 1) : cut + '…')
}

function jumpNote(n) {
  const p = paraByIdx.value[n.para_idx]
  const span = p ? lineSpanOf(p, sentenceAround(p.text, n.quote) || n.quote) : null
  if (span) return jumpTo(n.page, span.bbox.y0, span.bbox.y1)
  if (n.rect) return jumpTo(n.page, n.rect.y0, n.rect.y1)
  jumpPara(n.para_idx)
}

/* ---------- 五个问题（「问题」页签） ----------
   段落角色退到幕后：页边书签、跳转定位一律照旧，
   但"图例 + 计数"那块 UI 换成读者真正会问的五个问题。
   故意不把答案摊开：问题先出现，点哪条才展开哪条。
   五条答案都在**首次析读时一次写完**并按篇缓存，进这一页就有，不需要点任何按钮。
   （原来的①要解决什么/②为什么要解决重合度太高——同一件事的两种说法，合成了一问。） */
const SIX = [
  { k: 'q1', n: 1, q: '要解决什么、为什么？', gen: 'motive' },
  { k: 'q2', n: 2, q: '怎么解决的？' },
  { k: 'q3', n: 3, q: '还有什么没解决？' },
  { k: 'q4', n: 4, q: '还能做什么？', gen: 'next' },
  { k: 'q5', n: 5, q: '换个学科怎么看？', gen: 'lens' },
]  // 问题文案在渲染处过 t()
const openSix = reactive({ q1: false, q2: false, q3: false, q4: false, q5: false })
function toggleSix(k) { openSix[k] = !openSix[k] }

const six = reactive({ motive: null, how: null, next: null, lens: null })
const sixBusy = reactive({ motive: false, how: false, next: false, lens: false })
/* 五问各自的"答没答出来"。免费的 ②③ 看骨架，①④⑤ 看有没有取过。
   综述的 ② 走模型生成的"谱系问"（研究型没有实验证据层，那条链对综述是空壳）。
   有答案的那一问，行首的编号是墨色（没答的是灰的）——不点开也知道哪几问已经落地。 */
const sixHas = computed(() => ({
  q1: !!six.motive?.text,
  q2: isReview.value ? !!six.how?.text : !!store.analysis.claims.length,
  q3: !!(limitParas.value.length + warnNotes.value.length),
  q4: !!six.next?.items?.length, q5: !!six.lens?.items?.length,
}))

async function loadSix() {
  Object.assign(six, { motive: null, how: null, next: null, lens: null })
  Object.keys(openSix).forEach(k => (openSix[k] = false))   // 换篇回到"只有问题"的样子
  if (!store.currentId) return
  const mine = paperEpoch()
  try {
    const r = await api.sixAnswers(store.currentId)
    if (!samePaper(mine)) return        // 回来时已经换篇：这是上一篇的答案
    delete r.problem; delete r.why
    if (r.lens && !r.lens.v) delete r.lens
    if (r.next && !r.next.v) delete r.next
    Object.assign(six, r)
  } catch { /* 没缓存很正常 */ }
  const wanted = ['motive', 'next', 'lens'].concat(isReview.value ? ['how'] : [])
  for (const k of wanted) {
    if (six[k] || sixBusy[k]) continue
    sixBusy[k] = true
    api.sixAnswer(store.currentId, k)
      .then(v => { if (samePaper(mine)) six[k] = v })
      .catch(() => { /* 没配 key / 模型不给：保持"未生成" */ })
      .finally(() => { sixBusy[k] = false })
  }
}
function openMethod() {
  tab.value = 'eye'
  if (!methodCard.value) genMethodCard()
}
/* 一眼卡上的「↗」：指到「问题」页对应的那一问去，展开并滚到它。
   一眼卡负责"三十秒知道这篇讲什么"，想深了顺着箭头走——**指针，不是把内容再抄一遍**。 */
function gotoSix(k) {
  tab.value = 'skeleton'
  openSix[k] = true
  /* 外层 Transition 是 out-in：先等旧页签退场完（leave 90ms），新内容才插得进 DOM */
  setTimeout(() => {
    const i = SIX.findIndex(s => s.k === k)
    const el = rbodyEl.value?.querySelectorAll('.six')[i]
    if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, 120)
}
function askIt(q) {
  if (!q) return
  store.askPrefill = { question: q, send: true }
}

/* 导师三问的「去回答」：把这道题连提纲一起递给提问页，让模型帮起草口头回答——
   组会前一晚最想要的那个按钮。 */
function askAdvisor(q, i) {
  store.askPrefill = {
    question: t('这是「导师三问」的第 {n} 问：「{q}」。请帮我组织一份口头回答提纲：先给结论，再给论据（标注依据段号 [¶n]），最后补一句最可能被追问的地方。',
                { n: i + 1, q: q.q }),
    send: true,
  }
}

/* 眉批跑到一半喊停：后端在块边界收手，已写完的批注保留。 */
async function stopMarginalia() {
  try { await api.marginaliaCancel(store.currentId) } catch { /* 状态轮询会自己归位 */ }
}
async function stopAnalyzeHere() {
  try { await api.analysisCancel(store.currentId) } catch { /* 同上 */ }
}

/* 同一份笔记的第二条路：不落盘、直接进剪贴板，粘进 Notion/Obsidian/组会文档。 */
const exportCopied = ref(false)
async function copyExport() {
  try {
    const md = await (await fetch(api.exportMdUrl(store.currentId))).text()
    if (await copyText(md)) {
      exportCopied.value = true
      setTimeout(() => (exportCopied.value = false), 1600)
    } else toast(t('复制失败，手动选吧'))
  } catch (e) { toast(t('复制失败：{m}', { m: e.message })) }
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
/* 档位开关只管 AI 眉批。读者自己钉的（查译/框选/自己写的）是读者资产，永远显示、
   不参与筛选——所以这里没有「我写的」这一档（用户原话：没有 AI 眉批也要能显示）。 */
const BANDS = [
  { k: 'good', zh: '值得读', color: '#1d4e5f' },
  { k: 'warn', zh: '要当心', color: '#b8462e' },
  { k: 'noise', zh: '可跳过', color: '#8e8a80' },
]
const bandCount = computed(() => {
  const m = { good: 0, warn: 0, noise: 0 }
  for (const n of mnotes.value) m[bandOf(n)] = (m[bandOf(n)] || 0) + 1
  return m
})
const bandOn = k => store.viewer.noteBands[k] !== false
const bandAny = computed(() => BANDS.some(b => bandOn(b.k)))
function toggleBand(k) {
  store.viewer.noteBands = { ...store.viewer.noteBands, [k]: !bandOn(k) }
}

function anchorsOf(claim) {
  return claim.anchors
    .map(idx => ({ idx, anno: store.analysis.annotations[String(idx)], para: paraByIdx.value[idx] }))
    .filter(x => x.anno)
}

const GENERIC = ['这篇论文解决什么问题？', '核心结论和最硬的证据是什么？', '方法上有什么可挑剔的地方？', '作者承认了哪些局限？']
const suggest = ref([])
async function loadSuggest() {
  if (!store.currentId || suggest.value.length) return
  const mine = paperEpoch()
  try {
    const r = await api.suggest(store.currentId)
    if (!samePaper(mine)) return
    suggest.value = r.questions || []
  } catch { /* 静默，回退到通用问题 */ }
}
const quickList = computed(() => (suggest.value.length ? suggest.value : GENERIC))

watch(() => store.askPrefill, pf => { if (pf) tab.value = 'ask' })
watch(() => store.askFocusTick, () => { tab.value = 'ask' })
watch(tab, t => { if (t === 'ask') store.askFocusTick++ })

const terms = ref([])
const termFilter = ref('')
const termForm = ref({ term_en: '', term_zh: '' })
const termsBusy = ref(false)
const termsTried = ref('')          // 已经替**哪一篇**试过生成（试失败的不再反复花钱）

async function loadTerms() {
  if (!store.currentId) { terms.value = []; return }
  const mine = paperEpoch()
  try {
    const items = await api.glossary(store.currentId)   // 术语是按篇的
    if (!samePaper(mine)) return                        // 等待期间换了篇：旧词表不落新篇
    terms.value = items
  } catch { /* 词表拉不到就当空表，用户仍可手添 */ }
  maybeGenTerms(mine)
}

/* 空表才补生成。**只在"确认这篇析读完了"之后**动手：store.analysis.status 是异步填的，
   换篇那一刻它还是上一篇的值，凭它判断会在刚导入、还没析读的论文上花掉一批 token。 */
async function maybeGenTerms(mine = paperEpoch()) {
  if (terms.value.length || termsBusy.value) return
  if (termsTried.value === store.currentId) return
  if (store.analysis.status !== 'done') return
  termsTried.value = store.currentId
  termsBusy.value = true
  try {
    const r = await api.glossaryGen(store.currentId)
    if (!samePaper(mine)) return
    terms.value = r.items || []
    if (r.abbrs) store.paper.abbrs = JSON.stringify(r.abbrs)   // 缩写是同一批的产物
  } catch (e) {
    if (samePaper(mine)) toast(t('术语没生成：{m}', { m: e.message }))
  } finally { termsBusy.value = false }
}
async function addTerm() {
  if (!termForm.value.term_en.trim() || !termForm.value.term_zh.trim()) return
  await api.glossaryAdd(store.currentId, { ...termForm.value, source: 'manual' })
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
  if (t === 'terms') loadTerms()      // 点进术语页：空表就补一次发掘（见 maybeGenTerms）
  if (t === 'ask') loadSuggest()
  if (t === 'eye') { loadFigures(); loadCachedBlocks() }
})
watch(() => store.analysis.status, s => {
  if (s !== 'done') return
  methodCard.value = null; advisor.value = []; suggest.value = []
  loadSuggest(); loadCachedBlocks()
  termsTried.value = ''              // 重算析读 = 词表也重发了一批，允许再补一次空白
  loadTerms()
  loadSix()          // 服务端重算析读时把五问的答案一并清了（answers_clear），
})
watch(() => store.marginalia.status, s => {
  if (s !== 'done') return
  advisor.value = []
  loadCachedBlocks()
  loadSix()          // ⑤「还能做什么」是从"有坑"的批注长出来的，眉批一换就得重取
})

/* ---------- 右栏宽度：拖动改，双击复位 ----------
   经典分栏拖动：位移直接写进 railW，宽度过渡由 CSS 负责；拖的过程中把过渡关掉
   （body.rail-resizing），松手再交还给 CSS，否则边缘会黏在手后面。
   键盘也能改（←/→），拖不动鼠标的人不该被挡在外面。 */
const RAIL_MIN = 268, RAIL_MAX = 760, RAIL_DEF = 336
/* 经典分栏拖动：位移直接写进 railW，宽度过渡由 CSS 负责；拖完才 reflow（不然每帧重画论文）。 */
const rail = useEdgeResize({
  get: () => store.viewer.railW, set: w => { store.viewer.railW = w },
  min: RAIL_MIN, max: RAIL_MAX, def: RAIL_DEF, dir: -1,      // 抓手在左缘：往左拖 = 变宽
  onEnd: () => { store.reflowTick++ },
})
const railDragging = rail.dragging
onUnmounted(() => { rail.end() })

const isReview = computed(() => store.paper?.paper_type === 'review')
const methodCard = ref(null)
const mcBusy = ref(false)
const mcMore = ref(false)          // 方法卡展开：默认只露前三步
const MC_STEPS = 3
const stepsShown = computed(() => {
  const all = methodCard.value?.steps || []
  /* 新口径下步骤句尾带依据段号 [¶n]——拆出来做成可点角标，跳回原文核对；
     老缓存没有段号就照旧整句渲染。 */
  const split = s => {
    const txt = String(s || '')
    const m = txt.match(/\[¶([0-9,，、\s]+)\]\s*$/)
    if (!m) return { text: txt, cites: [] }
    return { text: txt.slice(0, m.index).trim(), cites: (m[1].match(/\d+/g) || []).map(Number) }
  }
  return (mcMore.value ? all : all.slice(0, MC_STEPS)).map(split)
})
async function genMethodCard() {
  const mine = paperEpoch()
  mcBusy.value = true
  try {
    const r = await api.methodCard(store.currentId)
    if (!samePaper(mine)) return
    methodCard.value = r
  } catch (e) { toast(t('生成失败：{m}', { m: e.message })) } finally { mcBusy.value = false }
}
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
  await api.glossaryAdd(store.currentId, { term_en: a.en, term_zh: a.zh, source: 'abbr' })
  loadTerms()                     // 列表里少一条、下面的术语表多一条，动作可见
}
function eqq(idx) {
  const m = store.analysis.evidence_qs || {}
  return m[String(idx)] || ''
}

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
  } catch (e) { toast(t('生成失败：{m}', { m: e.message })) } finally { advBusy.value = false }
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

const figures = ref([])
const figuresLoading = ref(false)
const figIdx = ref(-1)
const lightbox = computed(() => (figIdx.value >= 0 ? figures.value[figIdx.value] || null : null))
const lbLoaded = ref(false)
/* 图注中文：灯箱打开时懒翻译一次（后端落库），本会话内按 idx 记住 */
const capZh = ref({})
const capPending = ref(false)
const capText = computed(() => lightbox.value ? (capZh.value[figIdx.value] || lightbox.value.caption || '') : '')
/* 长图注不值得挂成图下一根居中的文字墙：超过一段话的量就切成「左图右注」两栏 */
const capLong = computed(() => capText.value.length > 200)
/* 点遮罩关灯箱——但划选图注文字时 mouseup 落在遮罩上也会冒出 click，
   有选区就当没点：图注要能选中复制，不能一拖就整扇关掉 */
function overlayClick() {
  const s = window.getSelection()
  if (s && !s.isCollapsed) return
  figIdx.value = -1
}
watch(() => figIdx.value, i => {
  lbLoaded.value = false
  if (i >= 0) fetchCap(i)
})
watch(() => store.currentId, () => { capZh.value = {} })
async function fetchCap(i) {
  const lb = figures.value[i]
  if (!lb || !lb.caption || capZh.value[i] !== undefined || isEn()) return
  capPending.value = true
  try {
    const r = await api.figCaption(store.currentId, i)
    if (r.zh && r.zh !== lb.caption) capZh.value = { ...capZh.value, [i]: r.zh }
  } catch { /* 没模型/失败就留在原文 */ }
  capPending.value = false
}
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
  const mine = paperEpoch()
  figuresLoading.value = true
  try {
    const r = await api.figures(store.currentId)
    if (!samePaper(mine)) return
    figures.value = r.figures || []
  } catch { /* 无图论文静默 */ }
  figuresLoading.value = false
}
async function askFigure(f) {
  try {
    const url = api.figureUrl(store.currentId, f, 150)
    const blob = await (await fetch(url)).blob()
    const img = await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(blob) })
    figIdx.value = -1
    const para = store.paras.find(p => p.page === f.page && p.bbox.y0 <= f.y1 && p.bbox.y1 >= f.y0)
    store.visPrefill = {
      img, question: t('讲解这张图：画了什么、支持论文的哪个结论、有什么可疑之处。'),
      page: f.page, paraIdx: para?.idx ?? 0, rect: { x0: f.x0, y0: f.y0, x1: f.x1, y1: f.y1 },
    }
  } catch (e) { toast(t('取图失败：{m}', { m: e.message })) }
}
function figJump(f) {
  jumpTo(f.page, f.y0, f.y1)
  figIdx.value = -1
}

/* 写笔记要贴图：把这张裁剪图复制/存下来，不用再截图 */
async function copyFig() {
  try {
    const blob = await (await fetch(api.figureUrl(store.currentId, lightbox.value, 200))).blob()
    await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })])
    toast(t('图片已复制'))
  } catch { toast(t('复制失败，手动选吧')) }
}
function downloadFig() {
  const name = (lightbox.value.label || lightbox.value.caption || 'figure').replace(/[\s/]+/g, '_').slice(0, 40)
  const a = document.createElement('a')
  a.href = api.figureUrl(store.currentId, lightbox.value, 300)
  a.download = `fig_p${lightbox.value.page + 1}_${name}.png`
  a.click()
}

/* 术语表是全库共用的，但"跳去原文"这件事**只对本文出现过的词成立**：
   别的论文的术语在这里点 ↗ 必然查不到（用户报的"几乎都查不到原文"就是这个）。
   所以先算一次本文正文（归一化：折连字、只留字母数字与汉字），只给命中的词出箭头，
   并把它们排在前面——一眼能看出哪些是这篇的词。 */
const LIGFOLD = { 'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi',
                  'ﬄ': 'ffl', 'ﬅ': 'ft', 'ﬆ': 'st' }
function fold(s) {
  let out = ''
  for (const ch of (s || '').toLowerCase()) out += LIGFOLD[ch] || ch
  return out.replace(/[^0-9a-z一-鿿]+/g, '')
}
const paperNorm = computed(() => fold(store.paras.map(p => p.text || '').join(' ')))
function inPaper(t) {
  const q = fold(t?.term_en)
  return q.length >= 3 && paperNorm.value.includes(q)
}
const termsFiltered = computed(() => {
  const f = termFilter.value.trim().toLowerCase()
  let list = [...terms.value].sort((a, b) => (inPaper(b) ? 1 : 0) - (inPaper(a) ? 1 : 0)
                                            || a.term_en.localeCompare(b.term_en))
  if (!f) return list
  list = list.filter(t => t.term_en.toLowerCase().includes(f) || t.term_zh.includes(f))
  return list
})
function findTerm(en) {
  if (!en) return
  store.viewerApi?.findInPaper(en)
}

/* 换篇：所有"按篇"的东西都要清干净——它们都带 `if (已有) return` 守卫，不清就会
   **永远**留在新论文上（比闪一下更难发现）。 */
watch(() => store.currentId, () => {
  tab.value = 'skeleton'
  methodCard.value = null
  mcMore.value = false
  advisor.value = []
  figures.value = []
  suggest.value = []
  loadTerms()          // 术语按篇：换一篇就换一份词表
  loadSix()
}, { immediate: true })
</script>

<template>
  <aside class="rail-right">
        <div class="rail-grip" :class="{ on: railDragging }" role="separator" aria-orientation="vertical"
         tabindex="0" :title="t('拖动改宽度 · 双击复位 · ←→')"
         @mousedown="rail.start" @dblclick="rail.reset"
         @keydown.left.prevent="rail.nudge(28)" @keydown.right.prevent="rail.nudge(-28)"></div>
    <div class="rtabs">
      <button class="rt" :class="{ on: tab === 'skeleton' }" @click="tab = 'skeleton'">{{ t('问题') }}</button>
      <button class="rt" :class="{ on: tab === 'eye' }" @click="tab = 'eye'">{{ t('速览') }}</button>
      <button class="rt" :class="{ on: tab === 'ask' }" @click="tab = 'ask'">{{ t('提问') }}</button>
      <button v-if="!isEn()" class="rt" :class="{ on: tab === 'terms' }" @click="tab = 'terms'">{{ t('术语') }}</button>
      <button class="rt-collapse" :title="t('收起右栏（x）')" @click="store.viewer.railUser = false">»</button>
    </div>
    <div class="rbody" ref="rbodyEl" :class="{ flush: tab === 'ask' }">
            <Transition name="rt" mode="out-in">
      <div class="rt-pane" v-if="tab !== 'ask'" :key="tab">
            <template v-if="tab === 'skeleton'">
        <div class="reading" v-if="store.analysis.status === 'running'">
          <div class="r-line">{{ t('正在通读…') }}</div>
          <div class="r-bar"><i /></div>
          <div class="blk-prog-line" v-if="store.analysis.progress?.total">
            <span>{{ t('已完成 {n}/{m} 项', { n: store.analysis.progress.done, m: store.analysis.progress.total }) }}</span>
            <button class="lnk" @click="stopAnalyzeHere">{{ t('停止') }}</button>
          </div>
        </div>
        <div v-else-if="store.analysis.status === 'error'" style="padding:8px 2px">
          <div style="font-size:var(--fs-sm);color:var(--vermilion);line-height:1.6">{{ t(store.analysis.error) }}</div>
          <button style="margin-top:10px" @click="emit('analyze')">{{ t('重试') }}</button>
        </div>
                <div v-else-if="store.analysis.status !== 'done'" style="padding:8px 2px">
          <div style="font-size:var(--fs-md);line-height:1.75;color:var(--ink-2)">
            {{ store.paras.length ? t('还没析读：析读后才有这五个答案。') : t('扫描件：能读、能框选问 AI，五问答不了。') }}
          </div>
        </div>

        <template v-else-if="!store.paras.length">
          <div class="r-note">{{ t('扫描件：能读、能框选问 AI，五问答不了。') }}<span v-if="figures.length">{{ t(' 速览页有 {n} 张图表。', { n: figures.length }) }}</span></div>
        </template>

        <template v-else>
                    <div class="six-head">
            <span v-if="store.readingPara" class="mono-num">{{ t('读至 ¶{n} / {m}', { n: store.readingPara, m: store.paras.length }) }}</span>
          </div>

          <section class="six" v-for="s in SIX" :key="s.k" :class="{ open: openSix[s.k] }">
            <button class="six-q" @click="toggleSix(s.k)">
              <i :class="{ on: sixHas[s.k] }">{{ s.n }}</i><span class="qt">{{ t(s.k === 'q3' && isReview ? '它把文献怎么组织的？' : s.q) }}</span>
            </button>

                        <div class="six-fold" :class="{ open: openSix[s.k] }">
             <div class="six-fold-in">
            <div class="six-a">
                            <template v-if="s.k === 'q1'">
                <MdLite v-if="six.motive?.text" class="six-txt" :text="six.motive.text" @cite="c => jumpPara(c.n)" />
                <div v-else class="six-note">{{ sixBusy.motive ? '…' : t('未生成') }}</div>
              </template>

                            <template v-else-if="s.k === 'q2' && isReview">
                <MdLite v-if="six.how?.text" class="six-txt" :text="six.how.text" @cite="c => jumpPara(c.n)" />
                <div v-else class="six-note">{{ sixBusy.how ? '…' : t('未生成') }}</div>
                <div class="six-foot">
                  <button @click="openMethod">{{ t('谱系卡 ↗') }}</button>
                </div>
              </template>
              <template v-else-if="s.k === 'q2'">
                <div class="claim-item" v-for="c in store.analysis.claims" :key="c.id">
                  <div class="c-head" @click="c.anchors.length && jumpPara(c.anchors[0])">
                    <span class="c-id">{{ c.id }}</span>
                    <span class="c-txt">{{ c.text }}</span>
                  </div>
                  <div class="ev-row" v-for="a in anchorsOf(c)" :key="a.idx" @click="jumpPara(a.idx)">
                    <span class="e-dot">¶{{ a.idx }}</span>
                    <span class="e-bar" :style="{ background: ROLE_COLOR[a.anno.role] }"></span>
                    <span class="e-note">
                      <span class="rg-kind" :style="{ color: ROLE_TEXT_COLOR[a.anno.role] }">{{ t(ROLE_ZH[a.anno.role]) }}</span>
                      {{ a.anno.purpose }}
                      <div class="ev-q" v-if="eqq(a.idx)">{{ t('该实验回答：') }}{{ eqq(a.idx) }}</div>
                    </span>
                  </div>
                  <div v-if="!anchorsOf(c).length" class="six-note">{{ t('未找到直接证据段') }}</div>
                </div>
                <div class="six-foot">
                  <button @click="openMethod">{{ t('方法卡 ↗') }}</button>
                </div>
              </template>

                            <template v-else-if="s.k === 'q3'">
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
                    <button class="ev-ask" @click.stop="askNotePrefill(n)">{{ t('问 ↗') }}</button>
                  </span>
                </div>
                <div class="six-note" v-if="!limitParas.length && !warnNotes.length">
                  {{ t('作者没明说局限，眉批也没标出可疑之处。') }}
                </div>
              </template>

                            <template v-else-if="s.k === 'q4'">
                <div class="six-item" v-for="(it, i) in (six[s.gen]?.items || [])" :key="i">
                  <div class="si-lead" v-if="it.lead">{{ it.lead }}</div>
                  <MdLite class="six-txt" :text="it.text" @cite="c => jumpPara(c.n)" />
                  <button class="si-ask" v-if="it.ask" @click="askIt(it.ask)">{{ it.ask }} ↗</button>
                </div>
                <div v-if="!six[s.gen]?.items?.length" class="six-note">{{ sixBusy[s.gen] ? '…' : t('未生成') }}</div>
              </template>

                            <template v-else-if="s.k === 'q5'">
                <div class="six-item" v-for="(it, i) in (six.lens?.items || [])" :key="i">
                  <div class="si-lead" v-if="it.lead">{{ it.lead }}</div>
                  <MdLite class="six-txt" :text="it.text" @cite="c => jumpPara(c.n)" />
                  <button class="si-ask" v-if="it.ask" @click="askIt(it.ask)">{{ it.ask }} ↗</button>
                </div>
                <div v-if="!six.lens?.items?.length" class="six-note">{{ sixBusy.lens ? '…' : t('未生成') }}</div>
              </template>
            </div>
             </div>
            </div>
          </section>

                    <div class="blk">
            <div class="blk-head">
              <span class="mono-label">{{ t('眉批') }}<span v-if="mnotes.length"> · {{ mnotes.length }}</span></span>
              <button v-if="store.marginalia.status !== 'running'" class="blk-get" @click="emit('marginalia')"
                      :title="mnotes.length ? t('替换现有眉批') : ''">
                {{ mnotes.length ? t('重写') : t('AI 眉批') }}
              </button>
              <span v-else class="blk-busy">{{ t('写批注中') }}<span class="r-dots">…</span>
                <button class="lnk" style="margin-left:8px" @click="stopMarginalia">{{ t('停止') }}</button></span>
            </div>
                        <div class="blk-prog" v-if="store.marginalia.status === 'running'">
              <div class="r-bar">
                <i :class="{ det: marginPct !== null }" :style="marginPct !== null ? { width: marginPct + '%' } : null" />
              </div>
              <div class="blk-prog-line">
                <span v-if="store.marginalia.progress?.total">{{ t('已读 {n}/{m} 块', { n: store.marginalia.progress.done, m: store.marginalia.progress.total }) }}</span>
                <span v-else>{{ t('正在通读全文') }}</span>
                <span class="blk-elapsed">{{ marginSecs }}s</span>
              </div>
            </div>
                        <div class="band-bar" v-if="mnotes.length">
              <button v-for="b in BANDS" :key="b.k" class="band-chip" :class="{ off: !bandOn(b.k) }"
                      @click="toggleBand(b.k)">
                <i class="bdot" :style="{ background: b.color }"></i>{{ t(b.zh) }}<span class="n">{{ bandCount[b.k] }}</span>
              </button>
            </div>
            <div class="band-alloff" v-if="mnotes.length && !bandAny">
              {{ t('AI 眉批三档都收起（你自己钉的还在）：纸面上没有批注 ·') }}
              <button class="lnk" @click="store.viewer.noteBands = { good: true, warn: true, noise: true }">{{ t('全开') }}</button>
            </div>
            <p class="blk-warn" v-if="store.marginalia.status === 'error' && store.marginalia.error">
              {{ t(store.marginalia.error) }}
            </p>
                        <p class="blk-warn" v-else-if="store.marginalia.error">{{ t(store.marginalia.error) }}</p>
          </div>
        </template>
      </template>

                  <template v-if="tab === 'eye'">
          <div v-if="store.summaryErr" class="r-note">{{ t(store.summaryErr) }}
            <button class="lnk" style="margin-left:6px" @click="reloadSummary()">{{ t('重试') }}</button>
          </div>
        <div v-else-if="!store.summary" class="reading">
          <div class="r-line">{{ t('正在写一眼卡…') }}</div>
          <div class="r-bar"><i /></div>
        </div>
        <div class="card-eye" v-else>
          <div class="ce-one">{{ prettyChem(store.summary.one_line) }}</div>
          <div class="ce-row go" @click="gotoSix('q2')" :title="t('去「问题」页第 2 问：主张与证据链')">
            <span class="ce-k">{{ t('发现') }}</span><span class="ce-v">{{ prettyChem(store.summary.findings) }}</span>
            <span class="ce-go">↗</span>
          </div>
          <div class="ce-kw"><button class="chip kw-chip" v-for="k in store.summary.keywords" :key="k"
                     :title="t('在文中查找')" @click="store.viewerApi?.findInPaper(k)">{{ k }}</button></div>
        </div>

                <div class="blk" v-if="figures.length || figuresLoading">
          <div class="blk-head">
            <span class="mono-label" v-if="figures.length">{{ t('图表速览 · {n}', { n: figures.length }) }}</span>
            <span class="blk-busy" v-else>{{ t('正在找图表…') }}</span>
          </div>
          <div class="fig-strip" v-if="figures.length">
            <span v-for="(f, i) in figures" :key="i" class="fig-cell"
                  :title="f.caption || `${t(f.kind === 'table' ? '表' : '图')} · ${t('第 {p} 页', { p: f.page + 1 })}`" @click="figIdx = i">
              <img class="fig-thumb" :src="api.figureUrl(store.currentId, f)" loading="lazy" decoding="async" alt="" />
              <i class="fig-kind">{{ f.kind === 'table' ? t('表') : t('图') }}</i>
              <span class="fig-cap" v-if="f.caption">{{ f.caption }}</span>
            </span>
          </div>
        </div>

                <div class="blk">
          <div class="blk-head">
            <span class="mono-label">{{ t(isReview ? '谱系卡' : '方法卡') }}</span>
            <button v-if="!methodCard?.goal && !mcBusy" class="blk-get" @click="genMethodCard">{{ t('获取') }}</button>
            <span v-else-if="mcBusy" class="blk-busy">{{ t('获取中…') }}</span>
          </div>
          <div class="card-eye" v-if="methodCard?.goal">
            <div class="ce-row"><span class="ce-k">{{ t(isReview ? '定位' : '目标') }}</span><span class="ce-v">{{ prettyChem(methodCard.goal) }}</span></div>
            <div class="ce-row"><span class="ce-k">{{ t(isReview ? '对象' : '体系') }}</span><span class="ce-v">{{ prettyChem(methodCard.system) }}</span></div>
            <div class="ce-row"><span class="ce-k">{{ t(isReview ? '范围' : '条件') }}</span><span class="ce-v">{{ prettyChem(methodCard.conditions) }}</span></div>
            <div class="ce-row"><span class="ce-k">{{ t(isReview ? '脉络' : '步骤') }}</span>
              <span class="ce-v">
                <div class="mc-step" :class="{ unfold: mcMore && i >= MC_STEPS }"
                     :style="mcMore && i >= MC_STEPS ? { animationDelay: (i - MC_STEPS) * 45 + 'ms' } : null"
                     v-for="(s, i) in stepsShown" :key="i">{{ i + 1 }}. {{ prettyChem(s.text) }}<button
                     v-for="c in s.cites" :key="c" class="md-cite" :title="t('跳到这一段')"
                     @click="jumpPara(c)">¶{{ c }}</button></div>
              </span>
            </div>
            <div class="ce-row" v-if="mcMore && methodCard.notes"><span class="ce-k">{{ t(isReview ? '入门' : '注意') }}</span><span class="ce-v">{{ prettyChem(methodCard.notes) }}</span></div>
            <button v-if="!mcMore && methodCard.steps.length > MC_STEPS" class="blk-more" @click="mcMore = true">
              {{ t('展开全部') }} {{ methodCard.steps.length }} {{ t(isReview ? '条' : '步') }}<span v-if="methodCard.notes"> · {{ t(isReview ? '入门' : '注意') }}</span>
            </button>
          </div>
        </div>

                <div class="blk">
          <div class="blk-head">
            <span class="mono-label">{{ t('导师三问') }}</span>
            <button v-if="!advisor.length && !advBusy && store.analysis.status === 'done'" class="blk-get"
                    @click="loadAdvisor">{{ t('获取') }}</button>
            <span v-else-if="advBusy" class="blk-busy">{{ t('获取中…') }}</span>
          </div>
          <div v-if="advisor.length">
            <div class="adv-item" v-for="(q, i) in advisor" :key="i">
              <div class="adv-q">Q{{ i + 1 }} · {{ prettyChem(q.q) }}</div>
              <ul class="adv-outline"><li v-for="o in q.outline" :key="o">{{ prettyChem(o) }}</li></ul>
              <button class="si-ask" @click="askAdvisor(q, i)">{{ t('去回答') }} ↗</button>
            </div>
          </div>
        </div>

                <div class="blk" style="display:flex;gap:8px;flex-wrap:wrap">
          <a class="exp-btn" :href="api.exportMdUrl(store.currentId)" download>{{ t('导出笔记 .md') }}</a>
          <button class="exp-btn" @click="copyExport">{{ exportCopied ? t('已复制') : t('复制 Markdown') }}</button>
        </div>
      </template>

            <template v-if="tab === 'terms'">
                <div style="margin-bottom:14px" v-if="abbrList.length">
          <div class="mono-label" style="margin-bottom:6px">{{ t('本文缩写 · {n}', { n: abbrList.length }) }}</div>
          <div class="abbr-list">
            <div class="term-row" v-for="a in abbrList" :key="a.en">
              <span class="t-en" :title="a.en">{{ a.en }}</span>
              <span class="t-arrow">→</span>
              <span class="t-zh" :title="a.zh">{{ a.zh }}</span>
              <button class="t-add" :title="t('收进术语表')"
                      @click="saveAbbr(a)">＋</button>
            </div>
          </div>
        </div>

        <div class="mono-label" style="margin-bottom:6px">{{ t('术语表') }} · {{ termsBusy ? t('发掘中…') : terms.length }}</div>
        <div class="term-form">
          <input type="text" v-model="termForm.term_en" :placeholder="t('英文')" />
          <input type="text" v-model="termForm.term_zh" :placeholder="t('中文')" />
          <button :title="t('添加')" @click="addTerm">＋</button>
        </div>
        <input type="text" v-model="termFilter" :placeholder="t('筛选…')" class="term-filter" />
        <div style="margin-bottom:10px"><a class="exp-btn" :href="api.glossaryCsvUrl(store.currentId)" download>{{ t('导出 CSV') }}</a></div>
        <div v-for="term in termsFiltered" :key="term.id" class="term-row">
          <span class="t-en" :title="term.term_en">{{ term.term_en }}</span>
          <span class="t-arrow">→</span>
          <span class="t-zh">{{ term.term_zh }}</span>
                    <button v-if="inPaper(term)" class="t-go" :title="t('在论文中查找该词')"
                  @click="findTerm(term.term_en)">↗</button>
          <span v-else class="t-no" :title="t('这篇论文的正文里没有这个词')">—</span>
          <button class="t-del" @click="delTerm(term.id)" :title="t('删除')">×</button>
        </div>
      </template>
      </div>
      </Transition>

      <Transition name="fade">
                  <div class="ask-layer" v-show="tab === 'ask'">
        <AskPanel :quick="quickList" />
      </div>
      </Transition>
    </div>

        <Transition name="fade">
    <div class="lightbox" :class="{ side: capLong }" v-if="lightbox" @click="overlayClick">
      <button class="lb-x" :title="t('关闭')" @click="figIdx = -1">
        <svg viewBox="0 0 10 10" width="11" height="11" aria-hidden="true">
          <path d="M1 1l8 8M9 1l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none" />
        </svg>
      </button>
      <div class="lb-stage" @click.stop>
        <button class="lb-nav" :disabled="figures.length < 2" :title="t('上一张（←）')" @click="figStep(-1)">‹</button>
        <span class="lb-imgwrap" :class="{ loading: !lbLoaded }">
          <span class="lb-loading" v-if="!lbLoaded">{{ t('正在提取原图…') }}</span>
          <img :src="api.figureUrl(store.currentId, lightbox, 200)" @load="lbLoaded = true" />
        </span>
        <button class="lb-nav" :disabled="figures.length < 2" :title="t('下一张（→）')" @click="figStep(1)">›</button>
      </div>
      <div class="lb-cap" v-if="lightbox.caption" @click.stop>
        <span>{{ capText }}</span>
        <span class="lb-cap-wait" v-if="capPending && !capZh[figIdx]">{{ t('正在翻译图注…') }}</span>
      </div>
      <div class="lb-actions" @click.stop>
        <span class="mono-label">{{ t(lightbox.kind === 'table' ? '表' : '图') }} · {{ figIdx + 1 }} / {{ figures.length }} · {{ t('第 {p} 页', { p: lightbox.page + 1 }) }}</span>
        <button @click="figJump(lightbox)">{{ t('在原文查看') }}</button>
        <button @click="askFigure(lightbox)">{{ t(lightbox.kind === 'table' ? '问这张表' : '问这张图') }}</button>
        <button @click="copyFig"> {{ t('复制') }}</button>
        <button @click="downloadFig">{{ t('下载') }}</button>
      </div>
    </div>
    </Transition>
  </aside>
</template>
