<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, store, toast, jumpTo, KIND_ZH, ROLE_ZH, ROLE_GLYPH, ROLE_COLOR, ROLE_TEXT_COLOR,
         KIND_COLOR, KIND_TEXT_COLOR, roleInk } from '../store'
import { lineSpanOf } from '../find'
import AskPanel from './AskPanel.vue'
import MdLite from './MdLite.vue'

const USER_KINDS = ['lookup', 'region']   // 用户自己钉的（查译、选区问答），不算 AI 眉批

const emit = defineEmits(['analyze', 'marginalia'])
const tab = ref('skeleton')

const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))

function jumpPara(idx) {
  const p = paraByIdx.value[idx]
  if (p) jumpTo(p.page, p.bbox.y0, p.bbox.y1)
}
// 眉批跳转：跳到这条批注引用的那句话，而不是它所在段的开头
function jumpNote(n) {
  const p = paraByIdx.value[n.para_idx]
  const span = p ? lineSpanOf(p, n.quote) : null
  if (span) return jumpTo(n.page, span.bbox.y0, span.bbox.y1)
  if (n.rect) return jumpTo(n.page, n.rect.y0, n.rect.y1)
  jumpPara(n.para_idx)
}

/* ---------- 六个问题（骨架页签） ----------
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

const six = reactive({ why: null, next: null, lens: null })
const sixBusy = reactive({ why: false, next: false, lens: false })
const Q_OF = { why: 'q2', next: 'q5', lens: 'q6' }

async function loadSix() {
  Object.assign(six, { why: null, next: null, lens: null })
  Object.keys(openSix).forEach(k => (openSix[k] = false))   // 换篇回到"只有问题"的样子
  if (!store.currentId) return
  try { Object.assign(six, await api.sixAnswers(store.currentId)) } catch { /* 没缓存很正常 */ }
}
async function genSix(key) {
  if (sixBusy[key]) return
  sixBusy[key] = true
  try {
    six[key] = await api.sixAnswer(store.currentId, key)
    openSix[Q_OF[key]] = true
  } catch (e) { toast(e.message) }
  sixBusy[key] = false
}
// 「方法卡」是"怎么解决的"那条的加深版：点一下跳到速览页并顺手取回（没取过才取）
function openMethod() {
  tab.value = 'eye'
  if (!methodCard.value) genMethodCard()
}
// 就一条批注追问：把批注和它引的原话一起交给模型，问题才问得准
function askNote(n) {
  const kind = KIND_ZH[n.kind] || n.kind
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
const gapParas = computed(() => parasOfRole(['gap']))
const limitParas = computed(() => parasOfRole(['limitation']))
const warnNotes = computed(() => store.marginalia.notes.filter(n => n.kind === 'warning'))

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
watch(tab, t => { if (t === 'ask') loadSuggest(); if (t === 'eye') loadFigures() })
watch(() => store.analysis.status, s => { if (s === 'done') loadSuggest() })

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
async function genMethodCard() {
  mcBusy.value = true
  try { methodCard.value = await api.methodCard(store.currentId) }
  catch (e) { toast('生成失败：' + e.message) }
  mcBusy.value = false
}
const abbrList = computed(() => {
  try {
    const abbrs = JSON.parse(store.paper?.abbrs || '{}')
    const saved = new Set(terms.value.map(t => t.term_en.toLowerCase()))
    return Object.entries(abbrs).map(([en, zh]) => ({ en, zh, saved: saved.has(en.toLowerCase()) }))
  } catch { return [] }
})
async function saveAbbr(a) {
  await api.glossaryAdd({ term_en: a.en, term_zh: a.zh, source: 'abbr' })
  a.saved = true
  loadTerms()
  toast(`「${a.en}」已收进术语表`)
}
function eqq(idx) {
  const m = store.analysis.evidence_qs || {}
  return m[String(idx)] || ''
}

// ---------- 导师三问 ----------
// 不主动预生成：这是要花 token 的一次调用，没点"获取"就不该发生
const advisor = ref([])
const advBusy = ref(false)
async function loadAdvisor() {
  if (advBusy.value || advisor.value.length) return
  advBusy.value = true
  try { const r = await api.advisor(store.currentId); advisor.value = r.questions || [] }
  catch (e) { toast('生成失败：' + e.message) }
  advBusy.value = false
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

watch(() => store.currentId, () => { tab.value = 'skeleton'; loadSix() }, { immediate: true })
</script>

<template>
  <aside class="rail-right">
    <!-- 分栏拖手：贴在右栏左缘，往左拖变宽 -->
    <div class="rail-grip" :class="{ on: railDragging }" role="separator" aria-orientation="vertical"
         tabindex="0" title="拖动改宽度 · 双击复位 · ←→"
         @mousedown="startRailResize" @dblclick="store.viewer.railW = RAIL_DEF; store.reflowTick++"
         @keydown.left.prevent="nudgeRail(28)" @keydown.right.prevent="nudgeRail(-28)"></div>
    <div class="rtabs">
      <button class="rt" :class="{ on: tab === 'skeleton' }" @click="tab = 'skeleton'">六问</button>
      <button class="rt" :class="{ on: tab === 'eye' }" @click="tab = 'eye'">速览</button>
      <button class="rt" :class="{ on: tab === 'ask' }" @click="tab = 'ask'">提问</button>
      <button class="rt" :class="{ on: tab === 'terms' }" @click="tab = 'terms'">术语</button>
      <button class="rt-collapse" title="收起右栏（x）" @click="store.viewer.railUser = false">»</button>
    </div>
    <div class="rbody" :class="{ flush: tab === 'ask' }">
      <!-- ============ 骨架 ============ -->
      <template v-if="tab === 'skeleton'">
        <div class="reading" v-if="store.analysis.status === 'running'">
          <div class="r-line">正在拆骨架<span class="r-dots">…</span></div>
          <div class="r-bar"><i /></div>
        </div>
        <div v-else-if="store.analysis.status === 'error'" style="padding:8px 2px">
          <div style="font-size:var(--fs-sm);color:var(--vermilion);line-height:1.6">{{ store.analysis.error }}</div>
          <button style="margin-top:10px" @click="emit('analyze')">重试</button>
        </div>
        <div v-else-if="store.analysis.status !== 'done'" style="padding:8px 2px">
          <div style="font-size:var(--fs-md);line-height:1.75;color:var(--ink-2)">
            {{ store.paras.length ? '还没有析读。' : '这份 PDF 没有可提取的文字层（多半是扫描件）。原文照样能读，图表也能框选问 AI，但这六个问题答不了。' }}
          </div>
          <button v-if="store.paras.length" class="primary" style="margin-top:12px" @click="emit('analyze')">析读全文</button>
        </div>

        <template v-else-if="!store.paras.length">
          <div class="r-note">这份 PDF 没有可提取的文字层（多半是扫描件）。原文照样能读，图表也能框选问 AI，但这六个问题答不了。<span v-if="figures.length"> 速览页有 {{ figures.length }} 张图可以看。</span></div>
        </template>

        <template v-else>
          <!-- 六个问题：读一篇论文该带着的问题。问题免费、答案点开才看 -->
          <div class="six-head">
            <span v-if="store.readingPara" class="mono-num">读至 ¶{{ store.readingPara }} / {{ store.paras.length }}</span>
          </div>

          <section class="six" v-for="s in SIX" :key="s.k" :class="{ open: openSix[s.k] }">
            <button class="six-q" @click="toggleSix(s.k)">
              <i>{{ s.n }}</i><span class="qt">{{ s.q }}</span>
              <b v-if="s.k === 'q1' && gapParas.length">{{ gapParas.length }}</b>
              <b v-else-if="s.k === 'q3' && store.analysis.claims.length">{{ store.analysis.claims.length }}</b>
              <b v-else-if="s.k === 'q4' && limitParas.length + warnNotes.length">{{ limitParas.length + warnNotes.length }}</b>
            </button>

            <div class="six-a" v-show="openSix[s.k]">
              <!-- ① 要解决什么：缺口段（作者自己点出的问题），没有就退回最大的一条主张 -->
              <template v-if="s.k === 'q1'">
                <div class="gap-row" v-for="p in gapParas" :key="p.idx" @click="jumpPara(p.idx)">
                  <span class="g-tag">¶{{ p.idx }}</span>
                  <span class="g-txt">{{ annoOf(p.idx).purpose || p.text.slice(0, 40) + '…' }}</span>
                </div>
                <div class="six-note" v-if="!gapParas.length && store.analysis.claims.length">
                  原文没有点明的缺口段。从主张看，它在解决：{{ store.analysis.claims[0].text }}
                </div>
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
                <div class="gap-row" v-for="p in limitParas" :key="p.idx" @click="jumpPara(p.idx)">
                  <span class="g-tag">¶{{ p.idx }}</span>
                  <span class="g-txt">{{ annoOf(p.idx).purpose || p.text.slice(0, 40) + '…' }}</span>
                </div>
                <div v-for="n in warnNotes" :key="'w' + n.id" class="ev-row" @click="jumpNote(n)">
                  <span class="e-dot">¶{{ n.para_idx }}</span>
                  <span class="e-bar" :style="{ background: KIND_COLOR[n.kind] || KIND_COLOR.warning }"></span>
                  <span class="e-note">
                    {{ n.note }}
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
          </section>

          <div v-if="store.marginalia.status === 'done' && store.marginalia.notes.some(n => !USER_KINDS.includes(n.kind))"
               style="margin-top:16px">
            <div class="mono-label" style="margin-bottom:8px">眉批速览 · {{ store.marginalia.notes.filter(n => !USER_KINDS.includes(n.kind)).length }} 条</div>
            <div v-for="n in store.marginalia.notes.filter(n => !USER_KINDS.includes(n.kind)).slice(0, 8)" :key="n.id"
                 class="ev-row" @click="jumpNote(n)">
              <span class="e-dot"></span>
              <span class="e-bar" :style="{ background: KIND_COLOR[n.kind] }"></span>
              <span class="e-note">
                <span class="mono-label">{{ KIND_ZH[n.kind] }}</span> {{ n.note }}
                <button class="ev-ask" title="就这条批注追问模型" @click.stop="askNote(n)">问 ↗</button>
              </span>
            </div>
          </div>
          <button v-else-if="store.marginalia.status !== 'done' && store.marginalia.status !== 'running'"
                  style="margin-top:16px" @click="emit('marginalia')">让师兄写眉批</button>
        </template>
      </template>

      <!-- ============ 速览 ============ -->
      <template v-if="tab === 'eye'">
        <div v-if="store.summaryErr" class="r-note">{{ store.summaryErr }}</div>
        <div v-else-if="!store.summary" class="reading">
          <div class="r-line">正在写一眼卡<span class="r-dots">…</span></div>
          <div class="r-bar"><i /></div>
        </div>
        <div class="card-eye" v-else>
          <div class="ce-one">{{ store.summary.one_line }}</div>
          <div class="ce-row"><span class="ce-k">贡献</span><span class="ce-v">{{ store.summary.contributions }}</span></div>
          <div class="ce-row"><span class="ce-k">方法</span><span class="ce-v">{{ store.summary.methods }}</span></div>
          <div class="ce-row"><span class="ce-k">发现</span><span class="ce-v">{{ store.summary.findings }}</span></div>
          <div class="ce-kw"><span class="chip" v-for="k in store.summary.keywords" :key="k">{{ k }}</span></div>
        </div>

        <!-- 方法卡 -->
        <div style="margin-top:16px">
          <div class="mono-label" style="margin-bottom:8px;display:flex;justify-content:space-between">
            <span>方法卡</span>
          </div>
          <div v-if="!methodCard">
            <button style="width:100%" @click="genMethodCard" :disabled="mcBusy"
                    title="把方法整理成可复现的 protocol">
              {{ mcBusy ? '获取中…' : '获取' }}
            </button>
          </div>
          <div class="card-eye" v-else>
            <div class="ce-row"><span class="ce-k">目标</span><span class="ce-v">{{ methodCard.goal }}</span></div>
            <div class="ce-row"><span class="ce-k">体系</span><span class="ce-v">{{ methodCard.system }}</span></div>
            <div class="ce-row"><span class="ce-k">条件</span><span class="ce-v">{{ methodCard.conditions }}</span></div>
            <div class="ce-row"><span class="ce-k">步骤</span>
              <span class="ce-v">
                <div class="mc-step" v-for="(s, i) in methodCard.steps" :key="i">{{ i + 1 }}. {{ s }}</div>
              </span>
            </div>
            <div class="ce-row" v-if="methodCard.notes"><span class="ce-k">注意</span><span class="ce-v">{{ methodCard.notes }}</span></div>
          </div>
        </div>

        <!-- 图表速览 -->
        <div style="margin-top:16px" v-if="figures.length">
          <div class="mono-label" style="margin-bottom:8px">图表速览 · {{ figures.length }}</div>
          <div class="fig-strip">
            <img v-for="(f, i) in figures" :key="i" class="fig-thumb" :src="api.figureUrl(store.currentId, f)"
                 :title="`第 ${f.page + 1} 页`" @click="figIdx = i" />
          </div>
        </div>

        <!-- 导师三问 -->
        <div style="margin-top:16px">
          <div class="mono-label" style="margin-bottom:8px">导师三问</div>
          <div v-if="!advisor.length">
            <button style="width:100%" @click="loadAdvisor" :disabled="advBusy"
                    title="生成最可能被问住的 3 个问题">
              {{ advBusy ? '获取中…' : '获取' }}
            </button>
          </div>
          <div v-else>
            <div class="adv-item" v-for="(q, i) in advisor" :key="i">
              <div class="adv-q">Q{{ i + 1 }} · {{ q.q }}</div>
              <ul class="adv-outline"><li v-for="o in q.outline" :key="o">{{ o }}</li></ul>
            </div>
          </div>
        </div>

        <!-- 导出 -->
        <div style="margin-top:16px;display:flex;gap:8px">
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
              <button v-if="!a.saved" class="t-del" style="font-size:var(--fs-sm)" title="收进术语表"
                      @click="saveAbbr(a)">＋</button>
              <span v-else class="mono-label">已收</span>
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
          <button class="t-del" @click="delTerm(t.id)" title="删除">×</button>
        </div>
      </template>

      <!-- ============ 提问 ============ -->
      <AskPanel v-if="tab === 'ask'" :quick="quickList" />
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
