<script setup>
/* 论文日历：哪天读了什么、哪天入了什么。
 *
 * 数据是自动的：打开论文时 touch 按天记一笔阅读日志（一篇一天一行），
 * 入库日期取 created_at。这里不做任何手动输入——读就是记录，没有别的仪式。
 * 面板与文库同侧互斥；点列表里的论文直接打开（openPaper 又会 touch，日历自证）。
 */
import { computed, ref, watch } from 'vue'
import { api, store, openPaper } from '../store'
import { t, ui } from '../i18n'

const open = computed(() => store.viewer.calOpen)
const y = ref(0), m = ref(0)          // 正在看的年/月
const sel = ref('')                   // 选中的日子 'YYYY-MM-DD'
const days = ref({})                  // { 'YYYY-MM-DD': {reads:[], added:[]} }
const busy = ref(false)

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
    out.push({ d, key, reads: e.reads?.length || 0, added: e.added?.length || 0 })
  }
  return out
})

const monthStat = computed(() => {
  const keys = Object.keys(days.value).filter(k => (days.value[k].reads?.length || 0) > 0)
  return { n: keys.length, m: keys.reduce((a, k) => a + days.value[k].reads.length, 0) }
})
const selEntry = computed(() => days.value[sel.value] || null)

async function load() {
  busy.value = true
  try {
    const r = await api.calendar(`${y.value}-${pad(m.value)}`)
    days.value = r.days || {}
  } catch { days.value = {} }
  busy.value = false
}

function show(yy, mm, select) {
  y.value = yy; m.value = mm
  load().then(() => {
    if (!select) return
    // 默认选中：今天在本月就选今天，否则选最近一个有阅读记录的日子
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

    <div class="cal-stat">{{ t('本月 {n} 天 · {m} 篇', monthStat) }}</div>

    <div class="cal-grid">
      <span class="cal-wd" v-for="w in weekdays" :key="w">{{ w }}</span>
      <template v-for="(c, i) in cells" :key="i">
        <span v-if="!c" class="cal-cell blank" />
        <button v-else class="cal-cell"
                :class="{ today: c.key === todayKey(), sel: c.key === sel }"
                :title="c.reads || c.added
                  ? t('读过 {r} 篇 · 新入库 {a} 篇', { r: c.reads, a: c.added }) : ''"
                @click="sel = c.key">
          <span class="n">{{ c.d }}</span>
          <span class="dots">
            <i v-if="c.reads" class="d read" />
            <i v-if="c.added" class="d add" />
          </span>
        </button>
      </template>
    </div>

    <div class="cal-day" v-if="sel && selEntry && (selEntry.reads.length || selEntry.added.length)">
      <template v-if="selEntry.reads.length">
        <div class="mono-label">{{ t('读过') }} · {{ selEntry.reads.length }}</div>
        <button class="cal-paper" v-for="p in selEntry.reads" :key="'r' + p.id" @click="openPaperAndClose(p.id)">
          {{ p.title }}
        </button>
      </template>
      <template v-if="selEntry.added.length">
        <div class="mono-label">{{ t('新入库') }} · {{ selEntry.added.length }}</div>
        <button class="cal-paper added" v-for="p in selEntry.added" :key="'a' + p.id" @click="openPaperAndClose(p.id)">
          {{ p.title }}
        </button>
      </template>
    </div>
    <div class="cal-empty" v-else-if="sel">{{ t('这一天没有记录') }}</div>
  </div>
</template>
