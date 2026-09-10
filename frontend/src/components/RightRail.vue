<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, store, jumpTo, KIND_ZH, ROLE_ZH } from '../store'

const emit = defineEmits(['analyze', 'marginalia'])
const tab = ref('skeleton')

const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))

function jumpPara(idx) {
  const p = paraByIdx.value[idx]
  if (p) jumpTo(p.page, p.bbox.y0, p.bbox.y1)
}

const gapParas = computed(() =>
  store.paras.filter(p => store.analysis.annotations[String(p.idx)]?.role === 'gap'))

const roleCounts = computed(() => {
  const c = {}
  for (const v of Object.values(store.analysis.annotations)) c[v.role] = (c[v.role] || 0) + 1
  return c
})

function anchorsOf(claim) {
  return claim.anchors
    .map(idx => ({ idx, anno: store.analysis.annotations[String(idx)], para: paraByIdx.value[idx] }))
    .filter(x => x.anno)
}

// ---------- 提问 ----------
const question = ref('')
const asking = ref(false)
const QUICK = ['这篇论文解决什么问题？', '核心结论和最硬的证据是什么？', '方法上有什么可挑剔的地方？', '作者承认了哪些局限？']

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

// 问答轻量 markdown：# 标题、**加粗**、- 列表，¶n 变成可点的引用
const qaSegs = computed(() => store.qa.map(m => {
  if (m.role !== 'assistant') return m
  const lines = []
  for (const raw of m.content.split('\n')) {
    let t = raw, head = false, bullet = false
    const hm = t.match(/^#{1,4}\s*(.*)$/)
    if (hm) { head = true; t = hm[1] }
    if (/^\s*[-*]\s+/.test(t)) { bullet = true; t = t.replace(/^\s*[-*]\s+/, '') }
    const runs = []
    for (const seg of t.split(/(\*\*[^*]+\*\*|¶\s*\d+)/)) {
      if (!seg) continue
      if (seg.startsWith('**') && seg.endsWith('**')) runs.push({ text: seg.slice(2, -2), bold: true })
      else if (/^¶\s*\d+$/.test(seg)) runs.push({ text: seg, cite: parseInt(seg.replace(/[^\d]/g, '')) })
      else runs.push({ text: seg })
    }
    lines.push({ head, bullet, runs })
  }
  return { ...m, lines }
}))

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
})
onUnmounted(() => window.removeEventListener('eggpaper:terms-prefill', onPrefill))

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
          <div style="font-size:12.5px;color:var(--vermilion);line-height:1.6">{{ store.analysis.error }}</div>
          <button style="margin-top:10px" @click="emit('analyze')">重试</button>
        </div>
        <div v-else-if="store.analysis.status !== 'done'" style="padding:8px 2px">
          <div class="serif" style="font-size:14.5px;line-height:1.7;color:var(--ink-2)">
            还没有析读。<br />「析读全文」会站在作者的视角，把主张、证据、对照和样板段都翻出来。
          </div>
          <button class="primary" style="margin-top:12px" @click="emit('analyze')">析读全文</button>
        </div>

        <template v-else>
          <div class="role-legend">
            <span v-for="(zh, k) in ROLE_ZH" :key="k" class="rl" :title="zh"
                  @click="jumpPara(parseInt(Object.keys(store.analysis.annotations).find(x => store.analysis.annotations[x].role === k)))">
              <i :style="{ background: `var(--r-${k === 'boilerplate' ? 'boiler' : k})` }"></i>{{ zh }}
              <b v-if="roleCounts[k]">{{ roleCounts[k] }}</b>
            </span>
          </div>

          <div class="mono-label" style="margin-bottom:8px">GAP · 作者的出发点</div>
          <div class="gap-node" v-for="p in gapParas" :key="p.idx">
            <div class="gap-row" @click="jumpPara(p.idx)">
              <span class="g-tag">¶{{ p.idx }}</span>
              <span class="g-txt">{{ store.analysis.annotations[String(p.idx)]?.purpose || p.text.slice(0, 40) + '…' }}</span>
            </div>
          </div>

          <div class="mono-label" style="margin:14px 0 8px">CLAIMS → EVIDENCE · 论证链</div>
          <div class="claim-block" v-for="c in store.analysis.claims" :key="c.id">
            <div class="c-head">
              <span class="c-id">{{ c.id }}</span>
              <span class="c-txt">{{ c.text }}</span>
            </div>
            <div class="ev-row" v-for="a in anchorsOf(c)" :key="a.idx" @click="jumpPara(a.idx)">
              <span class="e-dot">¶{{ a.idx }}</span>
              <span class="e-bar" :style="{ background: `var(--r-${a.anno.role === 'boilerplate' ? 'boiler' : a.anno.role})` }"></span>
              <span class="e-note">
                <span class="mono-label" style="font-size:9px">{{ ROLE_ZH[a.anno.role] }}</span>
                {{ a.anno.purpose }}
              </span>
            </div>
            <div v-if="!anchorsOf(c).length" style="font-size:11.5px;color:var(--ink-3);margin-top:6px">未找到直接证据段</div>
          </div>

          <div v-if="store.marginalia.status === 'done' && store.marginalia.notes.length"
               style="margin-top:16px">
            <div class="mono-label" style="margin-bottom:8px">眉批速览 · {{ store.marginalia.notes.length }} 条</div>
            <div v-for="n in store.marginalia.notes.slice(0, 8)" :key="n.id" class="ev-row" @click="n.rect && jumpTo(n.page, n.rect.y0, n.rect.y1)">
              <span class="e-dot"></span>
              <span class="e-bar" :style="{ background: `var(--k-${n.kind})` }"></span>
              <span class="e-note"><span class="mono-label" style="font-size:9px">{{ KIND_ZH[n.kind] }}</span> {{ n.note }}</span>
            </div>
          </div>
          <button v-else-if="store.marginalia.status !== 'done' && store.marginalia.status !== 'running'"
                  style="margin-top:16px" @click="emit('marginalia')">让师兄写眉批（逐句吐槽）</button>
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
      </template>

      <!-- ============ 提问 ============ -->
      <template v-if="tab === 'ask'">
        <div class="qa-quick">
          <button v-for="q in QUICK" :key="q" @click="ask(q)">{{ q }}</button>
        </div>
        <div class="qa-msg" v-for="(m, i) in qaSegs" :key="i" :class="m.role">
          <div class="q-role">{{ m.role === 'user' ? '你' : 'EGGPAPER' }}</div>
          <template v-if="m.lines">
            <div class="q-body">
              <template v-for="(ln, li) in m.lines" :key="li">
                <div :class="{ 'qa-head': ln.head, 'qa-bullet': ln.bullet }">
                  <template v-for="(r, ri) in ln.runs" :key="ri">
                    <button v-if="r.cite" class="qa-cite" @click="jumpPara(r.cite)">{{ r.text }}</button>
                    <b v-else-if="r.bold">{{ r.text }}</b>
                    <template v-else>{{ r.text }}</template>
                  </template>
                </div>
              </template>
            </div>
          </template>
          <div class="q-body" v-else>{{ m.content }}</div>
        </div>
        <div ref="qaEnd"></div>
        <div class="qa-input">
          <input type="text" v-model="question" placeholder="基于这篇论文提问…" @keydown.enter="ask(question)" />
          <button class="primary" @click="ask(question)" :disabled="asking">{{ asking ? '…' : '问' }}</button>
        </div>
      </template>

      <!-- ============ 术语 ============ -->
      <template v-if="tab === 'terms'">
        <div class="term-form">
          <input type="text" v-model="termForm.term_en" placeholder="英文" style="flex:1.2" />
          <input type="text" v-model="termForm.term_zh" placeholder="中文" style="flex:1" />
          <button @click="addTerm">＋</button>
        </div>
        <input type="text" v-model="termFilter" placeholder="筛选…" style="width:100%; margin-bottom:8px; font-size:12px" />
        <div v-for="t in termsFiltered" :key="t.id" class="term-row">
          <span class="t-en" :title="t.term_en">{{ t.term_en }}</span>
          <span class="t-arrow">→</span>
          <span class="t-zh">{{ t.term_zh }}</span>
          <span v-if="t.source === 'seed'" class="mono-label" style="font-size:8px">SEED</span>
          <button class="t-del" @click="delTerm(t.id)" title="删除">×</button>
        </div>
      </template>
    </div>
  </aside>
</template>
