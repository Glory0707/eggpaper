<script setup>
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import 'pdfjs-dist/web/pdf_viewer.css'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, store, toast, KIND_ZH, ROLE_ZH, CORE_ROLES } from '../store'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

defineEmits(['override'])

const GUTTER = 158
const ROLE_COLOR = {
  background: '#a89c85', gap: '#b8462e', claim: '#b0740d', evidence: '#55704d',
  control: '#637a8e', boilerplate: '#ab9166', extension: '#7b6e96', limitation: '#99505f',
}
const KIND_COLOR = {
  insight: '#b0740d', padding: '#a89c85', hedge: '#637a8e', redundant: '#8d8066',
  hype: '#b8462e', ai: '#7b6e96', warning: '#99505f', stiff: '#a89c85',
}

const deskEl = ref(null)
const ready = ref(false)
const zoom = ref(1)
const fitScale = ref(1)
const pagesMeta = ref([])
const hot = ref(null)
const flash = ref(null)

const canvases = ref([])
const textLayers = ref([])
const pageEls = ref([])
const doneKeys = new Set()      // 已按某 scale 渲染过的页
let passToken = 0

const sel = reactive({ visible: false, x: 0, y: 0, text: '', context: '', zh: '', hits: [], busy: false, err: '' })

let pdfDoc = null
let renderSeq = 0
let ro = null

const scale = computed(() => Math.min(2.2, Math.max(0.4, fitScale.value * zoom.value)))
const parasByPage = computed(() => {
  const m = {}
  for (const p of store.paras) (m[p.page] ||= []).push(p)
  return m
})
const paraByIdx = computed(() => Object.fromEntries(store.paras.map(p => [p.idx, p])))

const notesShown = computed(() => (store.viewer.layers.marginalia ? store.marginalia.notes : []))

function roleOf(p) {
  return store.viewer.layers.skeleton ? store.analysis.annotations[String(p.idx)]?.role : null
}
function purposeOf(p) {
  return store.analysis.annotations[String(p.idx)]?.purpose || ''
}
function isCore(p) {
  return CORE_ROLES.includes(roleOf(p) || 'background')
}

async function load() {
  ready.value = false
  pagesMeta.value = []
  doneKeys.clear()
  if (pdfDoc) { try { pdfDoc.destroy() } catch { /* */ } }
  const url = `/api/papers/${store.currentId}/pdf?variant=${store.viewer.variant}`
  pdfDoc = await pdfjsLib.getDocument(url).promise
  const metas = []
  for (let i = 1; i <= pdfDoc.numPages; i++) {
    const page = await pdfDoc.getPage(i)
    const vp = page.getViewport({ scale: 1 })
    metas.push({ w: vp.width, h: vp.height })
  }
  pagesMeta.value = metas
  await measure()
  await renderAll()
  ready.value = true
}

function measure() {
  if (!deskEl.value || !pagesMeta.value.length) return
  const w = deskEl.value.clientWidth - 60
  fitScale.value = (w - GUTTER) / pagesMeta.value[0].w
}

function scheduleRender() {
  clearTimeout(scheduleRender._t)
  scheduleRender._t = setTimeout(renderAll, 160)
}

async function renderAll() {
  const seq = ++passToken
  await nextTick()
  for (let i = 0; i < pagesMeta.value.length; i++) {
    if (seq !== passToken) return
    await renderPage(i)
  }
}

async function renderPage(i) {
  const key = i + ':' + scale.value.toFixed(3)
  if (doneKeys.has(key)) return
  const meta = pagesMeta.value[i]
  const canvas = canvases.value[i]
  const tlEl = textLayers.value[i]
  const pageEl = pageEls.value[i]
  if (!canvas || !pageEl || !pdfDoc) return
  const page = await pdfDoc.getPage(i + 1)
  const viewport = page.getViewport({ scale: scale.value })
  const dpr = Math.min(2.5, window.devicePixelRatio || 1)
  canvas.width = Math.floor(viewport.width * dpr)
  canvas.height = Math.floor(viewport.height * dpr)
  canvas.style.width = viewport.width + 'px'
  canvas.style.height = viewport.height + 'px'
  pageEl.style.setProperty('--scale-factor', scale.value)
  const ctx = canvas.getContext('2d', { alpha: false })
  ctx.fillStyle = '#fff'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  await page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null }).promise
  if (tlEl) {
    tlEl.innerHTML = ''
    const tl = new pdfjsLib.TextLayer({ textContentSource: page.streamTextContent(), container: tlEl, viewport })
    await tl.render()
  }
  doneKeys.add(key)
}

function rectStyle(p) {
  const b = p.bbox
  return { left: b.x0 * scale.value + 'px', top: b.y0 * scale.value + 'px',
           width: (b.x1 - b.x0) * scale.value + 'px', height: (b.y1 - b.y0) * scale.value + 'px' }
}

// 页边标签防重叠堆叠
function tabsOnPage(pno) {
  const out = []
  let prevBottom = -1
  for (const p of parasByPage.value[pno] || []) {
    const role = roleOf(p)
    if (!role) continue
    let top = p.bbox.y0 * scale.value
    if (top < prevBottom + 3) top = prevBottom + 3
    prevBottom = top + 22
    out.push({ p, role, top })
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
    prevBottom = top + 92
    out.push({ n, top })
  }
  return out
}

// ---------- 划词 ----------
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
  const pno = pageEls.value.indexOf(pageEl)
  let context = ''
  if (pno >= 0) {
    const localY = r.top - pageEl.getBoundingClientRect().top
    for (const p of parasByPage.value[pno] || []) {
      if (localY >= p.bbox.y0 * scale.value - 4 && localY <= p.bbox.y1 * scale.value + 4) { context = p.text; break }
    }
  }
  Object.assign(sel, { visible: true, x: Math.min(window.innerWidth - 360, r.right + 10), y: Math.min(window.innerHeight - 220, r.top), text, context, zh: '', hits: [], busy: false, err: '' })
}

async function doTranslateSel() {
  sel.busy = true; sel.err = ''
  try {
    const r = await api.translateSelection(store.currentId, sel.text, sel.context)
    sel.zh = r.zh; sel.hits = r.hits
  } catch (e) { sel.err = e.message }
  sel.busy = false
}

function sendToGlossary() {
  store.glossaryPrefill = { term_en: sel.text.slice(0, 80), term_zh: sel.zh.replace('〔演示译文〕', '').slice(0, 24) }
  sel.visible = false
  toast('已带到术语表，请确认中文译法')
  window.dispatchEvent(new CustomEvent('eggpaper:terms-prefill'))
}

// ---------- 跳转 ----------
async function applyJump() {
  const j = store.jump
  if (!j || !deskEl.value) return
  await nextTick()
  const pageEl = pageEls.value[j.page]
  if (!pageEl) return
  const top = pageEl.offsetTop + j.y0 * scale.value - deskEl.value.clientHeight * 0.28
  deskEl.value.scrollTo({ top, behavior: 'smooth' })
  flash.value = null
  // 找到附近段落高亮
  for (const p of parasByPage.value[j.page] || []) {
    if (p.bbox.y0 * scale.value <= j.y0 * scale.value && p.bbox.y1 * scale.value >= j.y0 * scale.value - 6) {
      flash.value = p.idx
      break
    }
  }
  setTimeout(() => (flash.value = null), 2200)
}

onMounted(async () => {
  await load()
  // 观察视口容器（而非会随渲染长高的内容区），避免渲染反复重启
  ro = new ResizeObserver(() => { measure(); scheduleRender() })
  ro.observe(deskEl.value.parentElement || deskEl.value)
  document.addEventListener('mouseup', onMouseUp)
})
onBeforeUnmount(() => { ro?.disconnect(); document.removeEventListener('mouseup', onMouseUp) })

watch(() => store.viewer.variant, load)
watch(scale, () => { doneKeys.clear(); scheduleRender() })
watch(() => store.jump, applyJump)
watch(() => store.viewer.layers.skim, () => {})
</script>

<template>
  <div class="desk-inner" ref="deskEl">
    <div class="reading" v-if="!ready" style="max-width:420px;margin:60px auto">
      <div class="r-line">正在摆上书桌，铺开第 1 – {{ pdfDoc?.numPages || '…' }} 页<span class="r-dots">…</span></div>
      <div class="r-bar"><i /></div>
    </div>

    <div style="display:flex; flex-direction:column; align-items:center; gap:26px" v-else>
      <div class="page-wrap" v-for="(m, i) in pagesMeta" :key="i" :ref="el => (pageEls[i] = el)">
        <div class="page" :style="{ width: m.w * scale + 'px', height: m.h * scale + 'px' }">
          <canvas :ref="el => (canvases[i] = el)"></canvas>
          <div class="textLayer" :ref="el => (textLayers[i] = el)"></div>
          <div class="para-zone">
            <!-- 略读：非核心段蒙纱 -->
            <template v-for="p in parasByPage[i] || []" :key="'f' + p.idx">
              <div v-if="store.viewer.layers.skim && roleOf(p) && !isCore(p)"
                   class="para-fade" :class="{ hot: flash === p.idx }" :style="rectStyle(p)"></div>
              <div v-else-if="store.viewer.layers.skim && isCore(p)"
                   class="para-core-bar" :style="{ top: p.bbox.y0 * scale + 'px', height: (p.bbox.y1 - p.bbox.y0) * scale + 'px' }"></div>
              <div v-if="flash === p.idx" class="para-fade hot" :style="rectStyle(p)"></div>
            </template>
            <!-- 眉批：原文高亮 -->
            <template v-for="{ n } in notesOnPage(i)" :key="'n' + n.id">
              <div v-if="n.rect" class="mg-mark"
                   :style="{ left: n.rect.x0 * scale + 'px', top: n.rect.y0 * scale + 'px',
                             width: (n.rect.x1 - n.rect.x0) * scale + 'px', height: (n.rect.y1 - n.rect.y0) * scale + 'px',
                             background: KIND_COLOR[n.kind] + '2e',
                             borderBottom: '2px solid ' + KIND_COLOR[n.kind] + '99' }"></div>
            </template>
          </div>
        </div>

        <!-- 页边沟槽：角色标签 + 眉批 -->
        <div class="gutter" :style="{ height: m.h * scale + 'px' }">
          <div v-for="{ p, role, top } in tabsOnPage(i)" :key="'t' + p.idx" class="role-tab"
               :style="{ top: top + 'px', background: ROLE_COLOR[role] }"
               @mouseenter="hot = p.idx" @mouseleave="hot = null">
            <span class="pn">{{ p.idx }}</span>
            <div class="role-card" v-if="hot === p.idx" @mouseenter="hot = p.idx" @mouseleave="hot = null">
              <div class="rc-role" :style="{ color: ROLE_COLOR[role] }">{{ ROLE_ZH[role] }}</div>
              <div class="rc-purpose">「{{ purposeOf(p) || '推断中' }}」</div>
              <select :value="role" @change="e => $emit('override', { idx: p.idx, role: e.target.value })">
                <option value="">改判角色…</option>
                <option v-for="(zh, k) in ROLE_ZH" :key="k" :value="k">{{ zh }}</option>
              </select>
            </div>
          </div>
          <div v-for="{ n, top } in notesOnPage(i)" :key="'m' + n.id" class="mg-note" :style="{ top: top + 'px' }">
            <span class="mg-kind" :style="{ background: KIND_COLOR[n.kind] }">{{ KIND_ZH[n.kind] }}</span>
            <div>{{ n.note }}</div>
            <span class="mg-quote">“{{ n.quote.slice(0, 42) }}{{ n.quote.length > 42 ? '…' : '' }}”</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 缩放 -->
    <div v-if="ready" style="position:fixed; left:270px; bottom:22px; display:flex; gap:6px; z-index:80">
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
        <button style="padding:4px 10px" @click="sendToGlossary">收进术语表</button>
        <button class="ghost" style="padding:4px 8px" @click="sel.visible = false">×</button>
      </div>
    </div>
  </div>
</template>
