<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, store, toast, jumpTo, KIND_ZH, ROLE_ZH, ROLE_GLYPH, ROLE_COLOR, ROLE_TEXT_COLOR,
         KIND_COLOR, KIND_TEXT_COLOR, roleInk } from '../store'
import { lineSpanOf } from '../find'
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

const gapParas = computed(() =>
  store.paras.filter(p => store.analysis.annotations[String(p.idx)]?.role === 'gap'))

const roleCounts = computed(() => {
  const c = {}
  for (const v of Object.values(store.analysis.annotations)) c[v.role] = (c[v.role] || 0) + 1
  return c
})
// 每个角色到底落在哪几段。一个角色往往有十几段，"点一下跳第一个"等于把
// 其余位置全藏了——所以点开是摊开这一类的全部 ¶ 号，自己挑一个去。
const roleParas = computed(() => {
  const m = {}
  for (const [k, a] of Object.entries(store.analysis.annotations)) (m[a.role] ||= []).push(parseInt(k))
  for (const k of Object.keys(m)) m[k].sort((a, b) => a - b)
  return m
})
const openRole = ref(null)
function toggleRole(k) { openRole.value = openRole.value === k ? null : k }
// 排序本身就是信息：主干在前，铺垫在后
const ROLE_ORDER = ['claim', 'evidence', 'gap', 'limitation', 'control', 'extension', 'background', 'boilerplate']
const legendRoles = computed(() => ROLE_ORDER.filter(k => roleCounts.value[k]))

function anchorsOf(claim) {
  return claim.anchors
    .map(idx => ({ idx, anno: store.analysis.annotations[String(idx)], para: paraByIdx.value[idx] }))
    .filter(x => x.anno)
}

// ---------- 提问 ----------
// 提问框：预填与聚焦
const question = ref('')
const asking = ref(false)
const GENERIC = ['这篇论文解决什么问题？', '核心结论和最硬的证据是什么？', '方法上有什么可挑剔的地方？', '作者承认了哪些局限？']
const suggest = ref([])
const qaInput = ref(null)
async function loadSuggest() {
  if (!store.currentId || suggest.value.length) return
  try {
    const r = await api.suggest(store.currentId)
    suggest.value = r.questions || []
  } catch { /* 静默，回退到通用问题 */ }
}
const quickList = computed(() => (suggest.value.length ? suggest.value : GENERIC))

watch(() => store.askPrefill, pf => {
  if (!pf) return
  tab.value = 'ask'
  question.value = pf.paraIdx ? `¶${pf.paraIdx} 这段在说什么？` : `这段在说什么：「${pf.text}」？`
  store.askPrefill = null
  nextTick(() => qaInput.value?.focus())
})
watch(() => store.askFocusTick, () => { tab.value = 'ask'; nextTick(() => qaInput.value?.focus()) })

async function ask(q) {
  if (!q?.trim() || asking.value) return
  question.value = ''
  asking.value = true
  store.qa.push({ role: 'user', content: q, citations: [] })
  try {
    const r = await api.ask(store.currentId, q)
    store.qa.push({ role: 'assistant', content: r.answer, citations: r.citations })
  } catch (e) {
    store.qa.push({ role: 'assistant', content: '⚠ ' + e.message, citations: [] })
  }
  asking.value = false
  scrollQa()
}
function scrollQa() {
  setTimeout(() => qaEnd.value?.scrollIntoView({ behavior: 'smooth' }), 30)
}
const qaEnd = ref(null)

// 问答里的 ¶n 引用点击跳回原文（解析与排版在 MdLite 里）

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
const advisor = ref([])
const advBusy = ref(false)
async function loadAdvisor() {
  if (advBusy.value || advisor.value.length) return
  advBusy.value = true
  try { const r = await api.advisor(store.currentId); advisor.value = r.questions || [] }
  catch (e) { toast('生成失败：' + e.message) }
  advBusy.value = false
}
watch(() => store.analysis.status, s => { if (s === 'done') loadAdvisor() })

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

watch(() => store.currentId, () => { tab.value = 'skeleton' })
</script>

<template>
  <aside class="rail-right">
    <div class="rtabs">
      <button class="rt" :class="{ on: tab === 'skeleton' }" @click="tab = 'skeleton'">骨架</button>
      <button class="rt" :class="{ on: tab === 'eye' }" @click="tab = 'eye'">速览</button>
      <button class="rt" :class="{ on: tab === 'ask' }" @click="tab = 'ask'">提问</button>
      <button class="rt" :class="{ on: tab === 'terms' }" @click="tab = 'terms'">术语</button>
    </div>
    <div class="rbody">
      <!-- ============ 骨架 ============ -->
      <template v-if="tab === 'skeleton'">
        <div class="reading" v-if="store.analysis.status === 'running'">
          <div class="r-line">正在通读全文，找主张、证据与捷径<span class="r-dots">…</span></div>
          <div class="r-bar"><i /></div>
        </div>
        <div v-else-if="store.analysis.status === 'error'" style="padding:8px 2px">
          <div style="font-size:var(--fs-sm);color:var(--vermilion);line-height:1.6">{{ store.analysis.error }}</div>
          <button style="margin-top:10px" @click="emit('analyze')">重试</button>
        </div>
        <div v-else-if="store.analysis.status !== 'done'" style="padding:8px 2px">
          <div style="font-size:var(--fs-md);line-height:1.75;color:var(--ink-2)">
            还没有析读。
          </div>
          <button class="primary" style="margin-top:12px" @click="emit('analyze')">析读全文</button>
        </div>

        <template v-else>
          <!-- 图例就是页边那些色块的样本；点一类摊开它的全部位置 -->
          <div class="mono-label" style="margin:0 0 7px; display:flex; justify-content:space-between">
            <span>段落角色</span>
            <span v-if="store.readingPara">读至 ¶{{ store.readingPara }} / {{ store.paras.length }}</span>
          </div>
          <div class="role-legend">
            <span class="rl" v-for="k in legendRoles" :key="k" :class="{ on: openRole === k }" @click="toggleRole(k)">
              <i :style="{ background: ROLE_COLOR[k], color: roleInk(k) }">{{ ROLE_GLYPH[k] }}</i>
              {{ ROLE_ZH[k] }}
              <b>{{ roleCounts[k] }}</b>
            </span>
          </div>
          <div class="role-hits" v-if="openRole && roleParas[openRole]">
            <button v-for="idx in roleParas[openRole]" :key="idx" class="rh-chip" @click="jumpPara(idx)">¶{{ idx }}</button>
          </div>

          <div class="mono-label" style="margin-bottom:8px">研究缺口</div>
          <div class="gap-node" v-for="p in gapParas" :key="p.idx">
            <div class="gap-row" @click="jumpPara(p.idx)">
              <span class="g-tag">¶{{ p.idx }}</span>
              <span class="g-txt">{{ store.analysis.annotations[String(p.idx)]?.purpose || p.text.slice(0, 40) + '…' }}</span>
            </div>
          </div>

          <div class="mono-label" style="margin:14px 0 8px">论点与证据 · {{ store.analysis.claims.length }} 条</div>
          <div class="claim-item" v-for="c in store.analysis.claims" :key="c.id">
            <div class="c-head">
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
            <div v-if="!anchorsOf(c).length" style="font-size:var(--fs-sm);color:var(--ink-3);margin-top:6px">未找到直接证据段</div>
          </div>

          <div v-if="store.marginalia.status === 'done' && store.marginalia.notes.some(n => !USER_KINDS.includes(n.kind))"
               style="margin-top:16px">
            <div class="mono-label" style="margin-bottom:8px">眉批速览 · {{ store.marginalia.notes.filter(n => !USER_KINDS.includes(n.kind)).length }} 条</div>
            <div v-for="n in store.marginalia.notes.filter(n => !USER_KINDS.includes(n.kind)).slice(0, 8)" :key="n.id" class="ev-row" @click="jumpNote(n)">
              <span class="e-dot"></span>
              <span class="e-bar" :style="{ background: KIND_COLOR[n.kind] }"></span>
              <span class="e-note"><span class="mono-label">{{ KIND_ZH[n.kind] }}</span> {{ n.note }}</span>
            </div>
          </div>
          <button v-else-if="store.marginalia.status !== 'done' && store.marginalia.status !== 'running'"
                  style="margin-top:16px" @click="emit('marginalia')">让师兄写眉批</button>
        </template>
      </template>

      <!-- ============ 速览 ============ -->
      <template v-if="tab === 'eye'">
        <div v-if="!store.summary" class="reading">
          <div class="r-line">正在压出一眼卡<span class="r-dots">…</span></div>
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
            <button style="width:100%" @click="genMethodCard" :disabled="mcBusy">
              {{ mcBusy ? '整理中…' : '把方法整理成可复现的 protocol' }}
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
            <button style="width:100%" @click="loadAdvisor" :disabled="advBusy">
              {{ advBusy ? '推演中…' : '生成最可能被问住的 3 个问题' }}
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
      <template v-if="tab === 'ask'">
        <div class="qa-quick">
          <button v-for="q in quickList" :key="q" @click="ask(q)">{{ q }}</button>
        </div>
        <div class="qa-msg" v-for="(m, i) in store.qa" :key="i" :class="m.role">
          <div class="q-role">{{ m.role === 'user' ? '你' : 'EGGPAPER' }}</div>
          <MdLite v-if="m.role === 'assistant'" class="q-body" :text="m.content" @cite="jumpPara" />
          <div class="q-body" v-else>{{ m.content }}</div>
        </div>
        <div ref="qaEnd"></div>
          <div class="qa-input">
            <input ref="qaInput" type="text" v-model="question" placeholder="基于这篇论文提问…" @keydown.enter="ask(question)" />
            <button class="primary" @click="ask(question)" :disabled="asking">{{ asking ? '…' : '问' }}</button>
          </div>
      </template>

      <!-- ============ 术语 ============ -->
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
