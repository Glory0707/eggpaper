<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, KIND_ZH, ROLE_ZH, CORE_ROLES, KIND_COLOR, ROLE_COLOR } from '../store'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

defineEmits(['override'])

const GUTTER = 158
const LS_POS = 'eggpaper:pos:'

const deskEl = ref(null)
const ready = ref(false)
const zoom = ref(1)
const fitScale = ref(1)
const sheets = ref([])
const hoverPara = ref(null)      // 段落 hover：驱动把手
const hoverTab = ref(null)       // 标签 hover：驱动角色卡
const handleHold = ref(false)
const freshNotes = ref(false)
const backChip = ref(false)
const flash = ref(null)

const canvases = ref([]), textLayers = ref([]), pageEls = ref([])
const doneKeys = new Set()
let passToken = 0
const scroller = () => deskEl.value?.closest('.desk') || deskEl.value
let docs = { orig: null, dual: null, mono: null }
let ro = null

const scale = computed(() => Math.min(2.2, Math.max(0.4, fitScale.value * zoom.value)))
const parasByPage = computed(() => {
  const m = {}
  for (const p of store.paras) (m[p.page] ||= []).push(p)
  return m
})
const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))
const notesShown = computed(() => (store.viewer.layers.marginalia ? store.marginalia.notes : []))
const flatItems = computed(() => sheets.value.flatMap(s => s.items))
const tranReady = computed(() => store.paper?.translate_status === 'done')
const isSpread = computed(() => store.viewer.variant === 'dual' && store.viewer.spread === 'spread')

function roleOf(p) {
  return store.viewer.layers.skeleton ? store.analysis.annotations[String(p.idx)]?.role : null
}
function annoOf(p) { return store.analysis.annotations[String(p.idx)] }
function isCore(p) { return CORE_ROLES.includes(roleOf(p) || 'background') }

/* ---------------- 文档装载与 sheets 构建 ---------------- */

async function getDoc(kind) {
  if (!docs[kind]) docs[kind] = await pdfjsLib.getDocument(`/api/papers/${store.currentId}/pdf?variant=${kind}`).promise
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
    for (let i = 0; i < m.count; i++)
      push([{ key: `m${i}`, doc: 'mono', page: i, origPage: -1, w: m.w, h: m.h, margin: false, text: false }])
  } else if (v === 'dual') {
    const td = await getDoc('dual'), tm = await meta(td)
    if (store.viewer.spread === 'spread') {
      const od = await getDoc('orig'), om = await meta(od)
      for (let i = 0; i < om.count && 2 * i + 1 < tm.count; i++)
        push([
          { key: `sl${i}`, doc: 'orig', page: i, origPage: i, w: om.w, h: om.h, margin: false, text: true },
          { key: `sr${i}`, doc: 'dual', page: 2 * i + 1, origPage: -1, w: tm.w, h: tm.h, margin: false, text: false },
        ])
    } else {
      for (let j = 0; j < tm.count; j++) {
        const isOrig = j % 2 === 0
        push([{ key: `di${j}`, doc: 'dual', page: j, origPage: isOrig ? j : -1, w: tm.w, h: tm.h, margin: isOrig, text: isOrig }])
      }
    }
  }
  sheets.value = rows
}

async function load() {
  ready.value = false
  sheets.value = []
  doneKeys.clear()
  try {
    await buildSheets()
  } catch (e) {
    toast('文档加载失败：' + e.message); return
  }
  await measure()
  await renderAll()
  ready.value = true
  await nextTick()
  const pos = store.viewer.restorePos
  if (pos) { scroller().scrollTop = pos; store.viewer.restorePos = 0 }
}

/* ---------------- 渲染 ---------------- */

function measure() {
  const first = sheets.value[0]?.items?.[0]
  if (!deskEl.value || !first) return
  const perRow = sheets.value[0].items.length
  const gutterW = store.viewer.variant === 'original' ? GUTTER : 6
  fitScale.value = (deskEl.value.clientWidth - 60 - gutterW - (perRow === 2 ? 20 : 0)) / (first.w * perRow)
}

async function renderAll() {
  const seq = ++passToken
  await nextTick()
  for (let i = 0; i < flatItems.value.length; i++) {
    if (seq !== passToken) return
    await renderItem(flatItems.value[i])
  }
}

async function renderItem(it) {
  const key = it.key + ':' + scale.value.toFixed(3)
  if (doneKeys.has(key)) return
  const canvas = canvases.value[it.gi], tlEl = textLayers.value[it.gi], el = pageEls.value[it.gi]
  if (!canvas || !el) return
  const doc = await getDoc(it.doc)
  const page = await doc.getPage(it.page + 1)
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
      const tl = new pdfjsLib.TextLayer({ textContentSource: page.streamTextContent(), container: tlEl, viewport })
      await tl.render()
    }
    doneKeys.add(key)
  } catch (err) {
    console.error('[eggpaper] render fail', it.key, err)
  }
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

function tabsOnPage(pno) {
  const out = []
  let prevBottom = -1
  for (const p of parasByPage.value[pno] || []) {
    const role = roleOf(p)
    if (!role) continue
    let top = p.bbox.y0 * scale.value
    if (top < prevBottom + 3) top = prevBottom + 3
    prevBottom = top + 22
    out.push({ p, role, anno: annoOf(p), top })
  }
  return out
}

function notesOnPage(pno) {
  const out = []
  let prevBottom = -1
  for (const n of notesShown.value) {
    if (n.page !== pno) continue
    const anchor = n.rect ? n.rect.y0 : paraByIdx.value[n.para_idx]?.bbox.y0 || 0
    let top = anchor * scale.value
    if (top < prevBottom + 6) top = prevBottom + 6
    // 估算卡高：类型章 + 批注行数 + 引文行数，宁多勿叠
    const noteLines = Math.ceil((n.note || '').length / 9)
    const quoteLines = Math.ceil(Math.min(n.quote.length, 42) / 15)
    prevBottom = top + 46 + noteLines * 19 + quoteLines * 14
    out.push({ n, top })
  }
  return out
}

/* ---------------- 段落 hover 把手 ---------------- */

function onPageMove(e, it) {
  if (!it.text || it.origPage < 0) return
  const el = pageEls.value[it.gi]
  if (!el) return
  const localY = e.clientY - el.getBoundingClientRect().top
  for (const p of parasByPage.value[it.origPage] || []) {
    if (localY >= p.bbox.y0 * scale.value - 2 && localY <= p.bbox.y1 * scale.value + 2) {
      hoverPara.value = p.idx
      return
    }
  }
}

async function translateParaAndPin(idx) {
  try {
    const r = await api.translatePara(store.currentId, idx)
    if (!r.zh || !r.zh.trim()) { toast('模型没返回内容，再试一次'); return }
    const p = paraByIdx.value[idx]
    await api.pin(store.currentId, { quote: (p?.text || '').slice(0, 150), note: r.zh, para_idx: idx, page: p?.page ?? 0 })
    await refreshM()
    toast('译文已钉在页边')
  } catch (e) { toast('翻译失败：' + e.message) }
}

function askPara(idx) { store.askPrefill = { paraIdx: idx } }

/* ---------------- 划词 ---------------- */

const sel = reactive({ visible: false, x: 0, y: 0, text: '', context: '', paraIdx: 0, page: 0, zh: '', hits: [], busy: false, err: '' })

function onMouseUp(e) {
  const s = window.getSelection()
  if (!s || s.isCollapsed || !s.rangeCount) return
  const text = s.toString().trim()
  if (text.length < 2 || text.length > 3000 || !s.anchorNode) { sel.visible = false; return }
  const node = s.anchorNode.nodeType === 1 ? s.anchorNode : s.anchorNode.parentElement
  if (!node?.closest?.('.textLayer')) { sel.visible = false; return }
  const range = s.getRangeAt(0)
  const r = range.getBoundingClientRect()
  const pageEl = node.closest('.page')
  const it = flatItems.value.find(x => pageEls.value[x.gi] === pageEl)
  let context = '', paraIdx = 0, page = 0
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
}

async function doTranslateSel() {
  sel.busy = true; sel.err = ''
  try {
    const r = await api.translateSelection(store.currentId, sel.text, sel.context)
    sel.zh = r.zh; sel.hits = r.hits
  } catch (e) { sel.err = e.message }
  sel.busy = false
}

async function pinSel() {
  if (!sel.zh) await doTranslateSel()
  if (!sel.zh) return
  await api.pin(store.currentId, { quote: sel.text.slice(0, 150), note: sel.zh, para_idx: sel.paraIdx, page: sel.page })
  await refreshM()
  sel.visible = false
  toast('已钉在页边')
}

function sendToGlossary() {
  store.glossaryPrefill = { term_en: sel.text.slice(0, 80), term_zh: (sel.zh || '').replace('〔演示译文〕', '').slice(0, 24) }
  sel.visible = false
  toast('已带到术语表，请确认中文译法')
  window.dispatchEvent(new CustomEvent('eggpaper:terms-prefill'))
}

function askAboutSel() {
  store.askPrefill = { text: sel.text.slice(0, 80) }
  sel.visible = false
}

async function refreshM() {
  const m = await api.marginalia(store.currentId)
  Object.assign(store.marginalia, m)
}

async function unpin(mid) {
  await api.unpin(store.currentId, mid)
  await refreshM()
}

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
  const it = flatItems.value.find(x => x.origPage === j.page)
  const el = it && pageEls.value[it.gi]
  if (!el) return
  scroller().scrollTo({ top: el.offsetTop + j.y0 * scale.value - scroller().clientHeight * 0.28, behavior: 'smooth' })
  flash.value = null
  for (const p of parasByPage.value[j.page] || []) {
    if (p.bbox.y0 * scale.value <= j.y0 * scale.value && p.bbox.y1 * scale.value >= j.y0 * scale.value - 6) {
      flash.value = p.idx; break
    }
  }
  setTimeout(() => (flash.value = null), 2200)
  backChip.value = true
  clearTimeout(applyJump._t)
  applyJump._t = setTimeout(() => (backChip.value = false), 5000)
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
  const it = flatItems.value.find(x => x.origPage === p.page)
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
  else toast('滚动一下，告诉我你在读哪段')
}

store.viewerApi = { step, translateCurrent, jumpBack, translateSelectionKey }

/* ---------------- 滚动：scroll-spy + 位置记忆 ---------------- */

let spyT = null, saveT = null
function onScroll() {
  clearTimeout(spyT)
  spyT = setTimeout(() => {
    const focusY = scroller().scrollTop + scroller().clientHeight * 0.4
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
    spread: store.viewer.spread, zoom: zoom.value,
  }))
}

onMounted(async () => {
  await load()
  await nextTick()
  try {
    const saved = JSON.parse(localStorage.getItem(LS_POS + store.currentId) || '{}')
    if (saved.zoom) zoom.value = saved.zoom
  } catch { /* */ }
  ro = new ResizeObserver(() => { measure(); scheduleRender() })
  ro.observe(deskEl.value.parentElement || deskEl.value)
  document.addEventListener('mouseup', onMouseUp)
  scroller().addEventListener('scroll', onScroll, { passive: true })
})

onBeforeUnmount(() => {
  ro?.disconnect()
  document.removeEventListener('mouseup', onMouseUp)
  scroller()?.removeEventListener('scroll', onScroll)
  for (const d of Object.values(docs)) { try { d?.destroy() } catch { /* */ } }
  docs = { orig: null, dual: null, mono: null }
})

watch(() => store.viewer.variant, () => { doneKeys.clear(); load() })
watch(() => store.viewer.spread, () => { doneKeys.clear(); buildSheets().then(() => { measure(); renderAll() }) })
watch(scale, () => { doneKeys.clear(); scheduleRender() })
watch(() => store.jump, applyJump)
watch(() => store.marginalia.notes, (n, o) => {
  if (n.length && (!o || n.length > o.length)) {
    freshNotes.value = true
    setTimeout(() => (freshNotes.value = false), 1600)
  }
})
</script>

<template>
  <div class="desk-inner" ref="deskEl">
    <div class="reading" v-if="!ready" style="max-width:420px;margin:60px auto">
      <div class="r-line">正在摆上书桌<span class="r-dots">…</span></div>
      <div class="r-bar"><i /></div>
    </div>

    <div v-else style="display:flex; flex-direction:column; align-items:center; gap:26px">
      <div class="spread-row" v-for="(s, si) in sheets" :key="si">
        <div class="page-wrap" v-for="it in s.items" :key="it.key">
          <div class="page" :ref="el => (pageEls[it.gi] = el)"
               :style="{ width: it.w * scale + 'px', height: it.h * scale + 'px' }"
               @mousemove="e => onPageMove(e, it)"
               @mouseleave="() => { if (!handleHold) hoverPara = null }">
            <canvas :ref="el => (canvases[it.gi] = el)"></canvas>
            <div class="textLayer" v-if="it.text" :ref="el => (textLayers[it.gi] = el)"></div>

            <div class="para-zone">
              <template v-for="(p, pi) in parasByPage[it.origPage] || []" :key="'f' + p.idx">
                <div v-if="store.viewer.layers.skim && it.origPage >= 0 && roleOf(p) && !isCore(p)"
                     class="para-fade" :class="{ hot: flash === p.idx }"
                     :style="{ ...rectStyle(p), transitionDelay: Math.min(400, pi * 12) + 'ms' }"></div>
                <div v-else-if="store.viewer.layers.skim && it.origPage >= 0 && roleOf(p) && isCore(p)"
                     class="para-core-bar"
                     :style="{ top: p.bbox.y0 * scale + 'px', height: (p.bbox.y1 - p.bbox.y0) * scale + 'px' }"></div>
                <div v-if="flash === p.idx" class="para-fade hot" :style="rectStyle(p)"></div>
              </template>
              <template v-for="{ n } in notesOnPage(it.origPage)" :key="'n' + n.id">
                <div v-if="n.rect" class="mg-mark" :class="{ draw: freshNotes }"
                     :style="{ left: n.rect.x0 * scale + 'px', top: n.rect.y0 * scale + 'px',
                               width: (n.rect.x1 - n.rect.x0) * scale + 'px', height: (n.rect.y1 - n.rect.y0) * scale + 'px',
                               background: KIND_COLOR[n.kind] + '2e',
                               borderBottom: '2px solid ' + KIND_COLOR[n.kind] + '99' }"></div>
              </template>
            </div>

            <!-- 段落把手 -->
            <div class="para-handle" v-if="hoverPara != null && it.text"
                 :style="{ top: (paraByIdx[hoverPara]?.bbox.y0 ?? 0) * scale + 'px' }"
                 @mouseenter="handleHold = true" @mouseleave="() => { handleHold = false; hoverPara = null }">
              <button @click="translateParaAndPin(hoverPara)">译</button>
              <button @click="askPara(hoverPara)">问</button>
            </div>
          </div>

          <!-- 页边批注带 -->
          <div class="gutter" v-if="it.margin"
               :style="{ height: it.h * scale + 'px', background: 'var(--paper-deep)', borderLeft: '1px solid var(--hairline-soft)', marginLeft: '10px', paddingLeft: '6px', marginRight: '-6px' }">
            <div v-for="{ p, role, anno, top } in tabsOnPage(it.origPage)" :key="'t' + p.idx" class="role-tab"
                 :style="{ top: top + 'px', background: ROLE_COLOR[role] }"
                 @mouseenter="hoverTab = p.idx" @mouseleave="hoverTab = null">
              <span class="pn">{{ p.idx }}</span>
              <div class="role-card" v-if="hoverTab === p.idx" @mouseenter="hoverTab = p.idx" @mouseleave="hoverTab = null">
                <div class="rc-role" :style="{ color: ROLE_COLOR[role] }">
                  {{ ROLE_ZH[role] }}
                  <span v-if="anno.user_override" class="rc-flag">已改</span>
                </div>
                <div class="rc-purpose">「{{ anno.purpose || '推断中' }}」</div>
                <div class="rc-hint">推断 · 可改判</div>
                <select :value="role" @change="e => $emit('override', { idx: p.idx, role: e.target.value })">
                  <option value="">回到推断</option>
                  <option v-for="(zh, k) in ROLE_ZH" :key="k" :value="k">{{ zh }}</option>
                </select>
              </div>
            </div>
            <div v-for="{ n, top } in notesOnPage(it.origPage)" :key="'mg' + n.id"
                 class="mg-note" :class="{ fresh: freshNotes }" :style="{ top: top + 'px' }">
              <span class="mg-kind" :style="{ background: KIND_COLOR[n.kind] }">
                {{ n.kind === 'lookup' ? '你 · 查译' : KIND_ZH[n.kind] }}
              </span>
              <button v-if="n.kind === 'lookup'" class="mg-del" title="移除" @click="unpin(n.id)">×</button>
              <div>{{ n.note }}</div>
              <span class="mg-quote">“{{ n.quote.slice(0, 42) }}{{ n.quote.length > 42 ? '…' : '' }}”</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 缩放 -->
    <div v-if="ready" style="position:fixed; left:50%; transform:translateX(-50%); bottom:18px; display:flex; gap:6px; z-index:80">
      <button style="padding:4px 10px" @click="zoom = Math.max(0.5, zoom - 0.15)">－</button>
      <button style="padding:4px 10px; font-family:var(--mono); font-size:11px" @click="zoom = 1">{{ Math.round(zoom * 100) }}%</button>
      <button style="padding:4px 10px" @click="zoom = Math.min(2.5, zoom + 0.15)">＋</button>
    </div>

    <!-- 划词气泡 -->
    <div class="sel-pop" v-if="sel.visible" :style="{ left: sel.x + 'px', top: sel.y + 'px' }" @mouseup.stop>
      <div v-if="!sel.zh && !sel.busy && !sel.err" style="font-size:12px;color:var(--ink-3)">
        已选 {{ sel.text.length }} 字符
      </div>
      <div v-if="sel.busy" style="font-size:12px;color:var(--ink-3)">翻译中…</div>
      <div v-if="sel.err" style="font-size:12px;color:var(--vermilion)">{{ sel.err }}</div>
      <div class="sp-zh" v-if="sel.zh">{{ sel.zh }}</div>
      <div class="sp-hits" v-if="sel.hits.length">
        <span class="chip" v-for="h in sel.hits" :key="h.en">📌 {{ h.en }} → {{ h.zh }}</span>
      </div>
      <div class="sp-actions">
        <button class="primary" style="padding:4px 10px" @click="doTranslateSel">{{ sel.zh ? '重译' : '翻译' }}</button>
        <button style="padding:4px 10px" @click="pinSel">钉在页边</button>
        <button style="padding:4px 10px" @click="sendToGlossary">收进术语</button>
        <button style="padding:4px 10px" @click="askAboutSel">提问</button>
        <button class="ghost" style="padding:4px 8px" @click="sel.visible = false">×</button>
      </div>
    </div>

    <!-- 返回原位 -->
    <button class="back-chip" v-if="backChip" @click="jumpBack">返回原位 · Alt+←</button>
  </div>
</template>
