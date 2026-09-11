<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, store, toast, refreshPapers, openPaper, refreshAnalysis } from './store'
import PdfViewer from './components/PdfViewer.vue'
import LibPanel from './components/LeftRail.vue'
import RightRail from './components/RightRail.vue'
import SettingsModal from './components/SettingsModal.vue'
import EggMark from './components/EggMark.vue'

const showSettings = ref(false)
const dragOver = ref(false)
const wobble = ref(false)
const gPending = ref(false)
let pollTimer = null

const tranReady = computed(() => store.paper?.translate_status === 'done')

onMounted(async () => {
  store.settings = await api.settings()
  await refreshPapers()
  if (store.papers.length) openPaper(store.papers[0].id)
  pollTimer = setInterval(poll, 3000)
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  clearInterval(pollTimer)
  window.removeEventListener('keydown', onKey)
})

async function poll() {
  if (!store.currentId) return
  if (store.analysis.status === 'running') await refreshAnalysis()
  if (store.marginalia.status === 'running') await refreshMarginalia()
  const p = store.papers.find(x => x.id === store.currentId)
  if (p && p.translate_status === 'running') {
    const j = await api.translateStatus(store.currentId)
    if (j.status === 'done') { await refreshPapers(); toast('双语已生成，切「译文」或「双语」查看') }
    if (j.status === 'error') { await refreshPapers(); toast('整本翻译失败：' + (j.error || '').slice(0, 80)) }
  }
}

watch(() => store.analysis.status, (n, o) => {
  if (o === 'running' && n === 'done') {
    wobble.value = true
    setTimeout(() => (wobble.value = false), 600)
  }
})

async function doAnalyze() {
  if (!store.currentId) return
  await api.analyze(store.currentId)
  await refreshAnalysis()
}

async function onOverride({ idx, role }) {
  try {
    await api.overrideRole(store.currentId, idx, role)
    await refreshAnalysis()
    toast(role ? '已改判' : '已回到推断')
  } catch (e) { toast('改判失败：' + e.message) }
}

async function doMarginalia() {
  if (!store.currentId) return
  await api.marginaliaStart(store.currentId)
  await refreshMarginalia()
}

async function doTranslateFull() {
  if (!store.currentId) return
  try {
    await api.translateFull(store.currentId)
    await refreshPapers()
    toast('整本翻译已启动，完成后自动提示')
  } catch (e) { toast('启动失败：' + e.message) }
}

async function onPickFile(file) {
  if (!file) return
  toast('已导入，正在后台通读…')
  try {
    const r = await api.upload(file)
    await refreshPapers()
    await openPaper(r.paper.id)
    store.viewer.libOpen = false
  } catch (e) { toast('导入失败：' + e.message) }
}

async function saveSettings(body) {
  store.settings = await api.saveSettings(body)
  showSettings.value = false
  toast('设置已保存（仅本机）')
  if (store.currentId) refreshAnalysis()
}

const tranSt = computed(() => store.papers.find(x => x.id === store.currentId)?.translate_status || 'none')

/* ---------------- 键盘流 ---------------- */
function onKey(e) {
  const t = e.target
  if (t && (t.matches?.('input, textarea, select') || t.isContentEditable)) return
  if (e.altKey && e.key === 'ArrowLeft') { store.viewerApi?.jumpBack(); e.preventDefault(); return }
  if (e.key === 'Escape') {
    store.viewer.libOpen = false
    store.shortcutCard = false
    showSettings.value = false
    store.tourStop?.()
    store.viewer.frame = false     // 框选模式永远能一键退出
    store.escTick++                // PDF 侧的划词/框选/角色卡浮层收起
    return
  }
  if (gPending.value) {
    gPending.value = false
    if (e.key === 'l') { store.viewer.libOpen = true; e.preventDefault() }
    return
  }
  if (!store.paper) return
  switch (e.key) {
    case 'j': e.preventDefault(); store.viewerApi?.step(1); break
    case 'k': e.preventDefault(); store.viewerApi?.step(-1); break
    case 't': store.viewerApi?.translateCurrent(); break
    case 's': store.viewerApi?.translateSelectionKey(); break
    case 'f': store.viewer.layers.skim = !store.viewer.layers.skim; break
    case 'r': store.viewer.frame = !store.viewer.frame; break
    case '1': store.viewer.variant = 'original'; break
    case '2': if (tranSt.value === 'done') store.viewer.variant = 'mono'; break
    case '3': if (tranSt.value === 'done') store.viewer.variant = 'dual'; break
    case '/': e.preventDefault(); store.askFocusTick++; break
    case 'x': store.viewer.railUser = !store.viewer.railUser; break
    case 'g': gPending.value = true; setTimeout(() => (gPending.value = false), 700); break
    case '?': store.shortcutCard = !store.shortcutCard; break
  }
}
</script>

<template>
  <div class="app" @dragover.prevent="dragOver = true" @dragleave="dragOver = false" @drop.prevent="e => { dragOver = false; onPickFile(e.dataTransfer?.files?.[0]) }">
    <header class="topbar">
      <div class="wordmark" title="eggpaper">
        <EggMark class="egg" :class="{ wobble }" />
        <span class="name">eggpaper</span>
        <span class="vol">v0.1</span>
      </div>
      <div class="doc-head" v-if="store.paper">
        <div class="t">{{ store.paper.title || store.paper.filename }}</div>
        <div class="m">{{ store.paper.n_pages }} 页 · {{ store.paras.length }} 段</div>
      </div>
      <div class="doc-head" v-else>
        <div class="t">本地文献批注台</div>
      </div>
      <div class="actions" v-if="store.paper">
        <div class="segmented">
          <button :class="{ on: store.viewer.variant === 'original' }" @click="store.viewer.variant = 'original'">原文</button>
          <button :class="{ on: store.viewer.variant === 'mono' }" :disabled="tranSt !== 'done'" @click="store.viewer.variant = 'mono'">译文</button>
          <button :class="{ on: store.viewer.variant === 'dual' }" :disabled="tranSt !== 'done'" @click="store.viewer.variant = 'dual'">双语</button>
        </div>
        <div class="segmented mini" v-if="store.viewer.variant === 'dual'">
          <button :class="{ on: store.viewer.spread === 'spread' }" @click="store.viewer.spread = 'spread'">对开</button>
          <button :class="{ on: store.viewer.spread === 'interleave' }" @click="store.viewer.spread = 'interleave'">交替</button>
        </div>
        <button class="toggle" :class="{ on: store.viewer.layers.skim }" @click="store.viewer.layers.skim = !store.viewer.layers.skim">略读</button>
        <button class="toggle" :class="{ on: store.viewer.frame }" title="框选任意区域问 AI（r）"
                @click="store.viewer.frame = !store.viewer.frame">框选</button>
        <button @click="doTranslateFull" :disabled="tranSt === 'running'">整本翻译</button>
        <button class="primary" @click="doAnalyze" :disabled="store.analysis.status === 'running'">
          {{ store.analysis.status === 'running' ? '通读中…' : (store.analysis.status === 'done' ? '重新析读' : '析读') }}
        </button>
      </div>
      <div class="actions">
        <span class="status-dot" :class="store.mock ? 'warn' : 'ok'" :title="store.mock ? '演示模式' : 'LLM 已配置'"></span>
        <button class="ghost" @click="showSettings = true" title="设置">⚙</button>
      </div>
    </header>

    <div class="main">
      <!-- 左：图标条 -->
      <div class="left-strip">
        <button class="strip-btn" :class="{ on: store.viewer.libOpen }" title="文库 · g l"
                @click="store.viewer.libOpen = !store.viewer.libOpen">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <path d="M4 4h6v16H4zM14 4h6v16h-6z" />
            <path d="M7 8h.01M7 12h.01M17 8h.01M17 12h.01" stroke-linecap="round" stroke-width="2.4" />
          </svg>
          <span class="badge" v-if="store.papers.length">{{ store.papers.length }}</span>
        </button>
        <div class="strip-sep"></div>
      </div>

      <!-- 中：书桌 -->
      <main class="desk" @drop.stop>
        <div class="empty" v-if="!store.paper">
          <EggMark class="egg-big" :class="{ open: dragOver }" />
          <div class="e-title">剥开论文的壳，读论证的芯</div>
          <div class="e-sub">把 PDF 拖进来，或按 g l 打开文库</div>
          <div class="stamp">EGGPAPER · LOCAL-FIRST</div>
        </div>
        <PdfViewer v-else :key="store.currentId" @override="onOverride" />
      </main>

      <!-- 右栏折叠把手 -->
      <button class="rail-tab" v-if="store.paper && !store.railRight" title="展开右栏 · x"
              @click="store.viewer.railUser = true">◂</button>
      <div class="rail-wrap" :class="{ collapsed: !store.railRight }">
        <RightRail @analyze="doAnalyze" @marginalia="doMarginalia" />
      </div>
    </div>

    <LibPanel v-if="store.viewer.libOpen" @pick="openPaper" @import="onPickFile" @close="store.viewer.libOpen = false" />
    <SettingsModal v-if="showSettings" @close="showSettings = false" @save="saveSettings" />

    <!-- 键盘卡 -->
    <div class="keys-card" v-if="store.shortcutCard" @click="store.shortcutCard = false">
      <div class="mono-label" style="margin-bottom:8px">键盘 · 按 ? 收起</div>
      <div class="k-row"><span>下一段 / 上一段（略读时仅核心段）</span><kbd>j / k</kbd></div>
      <div class="k-row"><span>译当前段并钉页边</span><kbd>t</kbd></div>
      <div class="k-row"><span>翻译划选</span><kbd>s</kbd></div>
      <div class="k-row"><span>略读</span><kbd>f</kbd></div>
      <div class="k-row"><span>框选问 AI（Esc 退出）</span><kbd>r</kbd></div>
      <div class="k-row"><span>原文 / 译文 / 双语</span><kbd>1 / 2 / 3</kbd></div>
      <div class="k-row"><span>聚焦提问</span><kbd>/</kbd></div>
      <div class="k-row"><span>折叠右栏</span><kbd>x</kbd></div>
      <div class="k-row"><span>文库</span><kbd>g l</kbd></div>
      <div class="k-row"><span>返回原位</span><kbd>Alt + ←</kbd></div>
      <div class="k-row"><span>收起所有浮层 / 退出框选</span><kbd>Esc</kbd></div>
    </div>

    <div class="toast" v-if="store.toast">{{ store.toast }}</div>
    <div class="modal-mask" v-if="dragOver && store.paper" style="pointer-events:none; background:rgba(38,32,21,.25)">
      <div class="modal" style="text-align:center">
        <div style="font-size:var(--fs-xl);font-weight:650">松手，放到书桌上</div>
      </div>
    </div>
  </div>
</template>
