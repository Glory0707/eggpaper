<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { api, store, openPaper, toast } from '../store'
import { t, ui } from '../i18n'

const open = computed(() => store.viewer.calOpen)
const y = ref(0), m = ref(0)          // 正在看的年/月
const sel = ref('')                   // 选中的日子 'YYYY-MM-DD'
const days = ref({})                  // { 'YYYY-MM-DD': {reads:[], added:[], plans:[]} }
const picking = ref(false)            // 「＋」的就地选论文
const pq = ref('')
const pickInput = ref(null)

const MONTHS_EN = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December']
const WD_ZH = ['一', '二', '三', '四', '五', '六', '日']
const WD_EN = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']
const weekdays = computed(() => (ui.lang === 'en' ? WD_EN : WD_ZH))

function monthLabel(yy, mm) {
  return ui.lang === 'en' ? `${MONTHS_EN[mm - 1]} ${yy}` : `${yy}年${mm}月`
}
const label = computed(() => monthLabel(y.value, m.value))

function pad(n) { return String(n).padStart(2, '0') }
function dayKey(yy, mm, d) { return `${yy}-${pad(mm)}-${pad(d)}` }
function todayKey() { const d = new Date(); return dayKey(d.getFullYear(), d.getMonth() + 1, d.getDate()) }

/* 月格子：周一开头，前后补空。 */
const cells = computed(() => {
  const first = new Date(y.value, m.value - 1, 1)
  const lead = (first.getDay() + 6) % 7
  const n = new Date(y.value, m.value, 0).getDate()
  const out = Array.from({ length: lead }, () => null)
  for (let d = 1; d <= n; d++) {
    const key = dayKey(y.value, m.value, d)
    const e = days.value[key] || {}
    out.push({ d, key, reads: e.reads?.length || 0, added: e.added?.length || 0, plans: e.plans?.length || 0 })
  }
  return out
})

const monthStat = computed(() => {
  const keys = Object.keys(days.value).filter(k => (days.value[k].reads?.length || 0) > 0)
  return { n: keys.length, m: keys.reduce((a, k) => a + days.value[k].reads.length, 0) }
})
/* 英文带单复数（"1 day · 1 read"），中文照走词典。 */
const statText = computed(() => {
  const { n, m } = monthStat.value
  if (ui.lang !== 'en') return t('本月 {n} 天 · {m} 篇', { n, m })
  return `${n} day${n === 1 ? '' : 's'} · ${m} read${m === 1 ? '' : 's'} this month`
})
const selEntry = computed(() => days.value[sel.value] || null)

/* ---- 待读：排到某天（＋），打开那篇自动消（后端 touch 顺手清），× 是反悔 ---- */
const pickList = computed(() => {
  const q = pq.value.trim().toLowerCase()
  return store.papers
    .filter(p => !q || (p.title || '').toLowerCase().includes(q) || (p.filename || '').toLowerCase().includes(q))
    .slice(0, 8)
})
function togglePick() {
  picking.value = !picking.value
  if (picking.value) nextTick(() => pickInput.value?.focus())
  else pq.value = ''
}
async function planIt(p) {
  if (p.plan_day === sel.value) return
  try {
    await api.plan(p.id, sel.value)
  } catch (e) { toast(e.message); return }
  p.plan_day = sel.value
  const e = (days.value[sel.value] = days.value[sel.value] || {})
  e.plans = [...(e.plans || []), { id: p.id, title: (p.title || p.filename || '').trim() }]
  picking.value = false
  pq.value = ''
}
async function unplan(pid) {
  try {
    await api.plan(pid, '')
  } catch (e) { toast(e.message); return }
  const e = days.value[sel.value]
  if (e) e.plans = (e.plans || []).filter(x => x.id !== pid)
  const p = store.papers.find(x => x.id === pid)
  if (p) p.plan_day = ''
}

async function load() {
  const want = `${y.value}-${pad(m.value)}`   // 快速翻月时响应会乱序：回来的不是当前月就丢掉
  try {
    const r = await api.calendar(want)
    if (`${y.value}-${pad(m.value)}` !== want) return
    days.value = r.days || {}
  } catch { if (`${y.value}-${pad(m.value)}` === want) days.value = {} }
}

function show(yy, mm, select) {
  y.value = yy; m.value = mm
  load().then(() => {
    if (!select) return
    const tk = todayKey()
    sel.value = tk.startsWith(`${yy}-${pad(mm)}`) && days.value[tk] ? tk
      : (Object.keys(days.value).filter(k => days.value[k].reads?.length).sort().pop() || '')
  })
}
function step(d) {
  let yy = y.value, mm = m.value + d
  if (mm === 0) { yy--; mm = 12 }
  if (mm === 13) { yy++; mm = 1 }
  show(yy, mm, false)
}
function goToday() {
  const d = new Date()
  show(d.getFullYear(), d.getMonth() + 1, true)
}
function openPaperAndClose(pid) {
  openPaper(pid)
  store.viewer.calOpen = false
}

/* 导出本月：组会/汇报"这个月读了哪些"直接要一份清单。前端就地拼 Markdown，零后端改动。 */
function exportMonth() {
  const keys = Object.keys(days.value).filter(k => (days.value[k]?.reads?.length || days.value[k]?.added?.length
    || days.value[k]?.plans?.length)).sort()
  if (!keys.length) return
  const lines = [`# ${label.value}`, '']
  for (const k of keys) {
    const e = days.value[k] || {}
    lines.push(`## ${k}`)
    for (const p of e.plans || []) lines.push(`- ${t('待读')}：${p.title}`)
    for (const r of e.reads || []) lines.push(`- ${t('读过')}：${r.title}`)
    for (const a of e.added || []) lines.push(`- ${t('新入库')}：${a.title}`)
    lines.push('')
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `eggpaper-${y.value}-${pad(m.value)}.md`
  a.click()
  URL.revokeObjectURL(a.href)
}

/* 面板是 v-if 挂进来的：挂上来那刻 open 已经是 true，必须 immediate 才接得住首拍 */
watch(open, v => {
  if (!v) return
  const d = new Date()
  show(d.getFullYear(), d.getMonth() + 1, true)
}, { immediate: true })
</script>

<template>
  <div class="lib-panel cal-panel">
    <div class="rail-head">
      <span class="mono-label">{{ t('论文日历') }}</span>
      <button class="ghost head-x" :title="t('收起日历')" @click="store.viewer.calOpen = false">‹</button>
    </div>

    <div class="cal-nav">
      <button class="cal-nav-btn" :title="t('上一个月')" @click="step(-1)">‹</button>
      <span class="cal-month">{{ label }}</span>
      <button class="cal-nav-btn" :title="t('下一个月')" @click="step(1)">›</button>
      <button class="cal-today" @click="goToday">{{ t('今天') }}</button>
    </div>

    <div class="cal-stat">
      <span>{{ statText }}</span>
      <button class="lnk" v-if="monthStat.m" @click="exportMonth">{{ t('导出本月 .md') }}</button>
    </div>

    <div class="cal-grid">
      <span class="cal-wd" v-for="w in weekdays" :key="w">{{ w }}</span>
      <template v-for="(c, i) in cells" :key="i">
        <span v-if="!c" class="cal-cell blank" />
        <button v-else class="cal-cell"
                :class="{ today: c.key === todayKey(), sel: c.key === sel }"
                :title="(c.plans ? t('待读 {n} 篇 · ', { n: c.plans }) : '') + (c.reads || c.added
                  ? t('读过 {r} 篇 · 新入库 {a} 篇', { r: c.reads, a: c.added }) : '')"
                @click="sel = c.key">
          <span class="n">{{ c.d }}</span>
          <span class="dots">
            <i v-if="c.plans" class="d plan" />
            <i v-if="c.reads" class="d read" />
            <i v-if="c.added" class="d add" />
          </span>
        </button>
      </template>
    </div>

    <div class="cal-day" v-if="sel">
      <template v-if="selEntry?.plans?.length">
        <div class="mono-label">{{ t('待读') }} · {{ selEntry.plans.length }}</div>
        <div class="cal-plan-row" v-for="p in selEntry.plans" :key="'p' + p.id">
          <button class="cal-paper plan" @click="openPaperAndClose(p.id)">{{ p.title }}</button>
          <button class="cal-x" @click="unplan(p.id)">×</button>
        </div>
      </template>
      <template v-if="selEntry?.reads?.length">
        <div class="mono-label">{{ t('读过') }} · {{ selEntry.reads.length }}</div>
        <button class="cal-paper" v-for="p in selEntry.reads" :key="'r' + p.id" @click="openPaperAndClose(p.id)">
          {{ p.title }}
        </button>
      </template>
      <template v-if="selEntry?.added?.length">
        <div class="mono-label">{{ t('新入库') }} · {{ selEntry.added.length }}</div>
        <button class="cal-paper added" v-for="p in selEntry.added" :key="'a' + p.id" @click="openPaperAndClose(p.id)">
          {{ p.title }}
        </button>
      </template>
      <div class="cal-empty" style="border:none;margin:0;padding:0"
           v-if="!selEntry || (!selEntry.plans?.length && !selEntry.reads?.length && !selEntry.added?.length)">
        {{ t('这一天没有记录') }}
      </div>
      <button class="cal-add" :class="{ on: picking }" @click="togglePick">＋</button>
      <div class="cal-picker" v-if="picking">
        <input ref="pickInput" v-model="pq" :placeholder="t('找一篇…')"
               @keydown.escape.prevent="picking = false" />
        <button class="cal-paper pick" v-for="p in pickList" :key="p.id"
                :disabled="p.plan_day === sel" @click="planIt(p)">
          <span class="t">{{ p.title || p.filename }}</span>
          <span v-if="p.plan_day === sel" class="ok">✓</span>
        </button>
      </div>
    </div>
  </div>
</template>
